#!/usr/bin/env bash
# Install the adjacent unmodified aws-execution-engine checkout without a justfile.
set -euo pipefail

ENGINE_ROOT="${1:?Usage: engine_install.sh <engine_repo_path>}"
PROJECT_PREFIX="${OPENCI_TF_PROJECT:-openci-tf}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="${PROJECT_PREFIX}-state-${ACCT}"
LOCK_TABLE="${PROJECT_PREFIX}-tf-locks"
PACKAGE_BUCKET="${PROJECT_PREFIX}-package-${ACCT}"
DONE_BUCKET="${PROJECT_PREFIX}-done-${ACCT}"
ENGINE_ZIP_S3_KEY="engine/artifacts/engine.zip"
SOPS_AGE_LAYER_S3_KEY="engine/artifacts/sops-age-layer.zip"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE_ROOT="$(cd "$ENGINE_ROOT" && pwd)"

if [ ! -f "${ENGINE_ROOT}/scripts/build-release-zip.sh" ]; then
  echo "ERROR: engine checkout missing scripts/build-release-zip.sh: ${ENGINE_ROOT}" >&2
  exit 1
fi

(
  cd "$ENGINE_ROOT"
  bash scripts/build-release-zip.sh
  aws s3 cp dist/engine.zip "s3://${STATE_BUCKET}/${ENGINE_ZIP_S3_KEY}"
  aws s3 cp dist/sops-age-layer.zip "s3://${STATE_BUCKET}/${SOPS_AGE_LAYER_S3_KEY}"
)

KMS_KEY_ARN="$(aws kms describe-key --key-id "alias/${PROJECT_PREFIX}-foundation" --query KeyMetadata.Arn --output text)"
[[ "$KMS_KEY_ARN" == arn:*:kms:*:*:key/* ]] || {
  echo "ERROR: foundation KMS alias did not resolve to a key ARN" >&2
  exit 1
}

export PROJECT_PREFIX
export ENGINE_ZIP_S3_BUCKET="$STATE_BUCKET"
export ENGINE_ZIP_S3_KEY
export SOPS_AGE_LAYER_S3_KEY
export KMS_KEY_ARN
export AWS_REGION="$REGION"
export ADDITIONAL_PACKAGE_BUCKET_ARNS_JSON="[\"arn:aws:s3:::${PACKAGE_BUCKET}\"]"
export ADDITIONAL_RESULT_BUCKET_ARNS_JSON="[\"arn:aws:s3:::${DONE_BUCKET}\"]"

DEPLOY_DIR="${ENGINE_ROOT}/infra/02-deploy"
CANONICAL_STATE_KEY="engine/terraform.tfstate"
LEGACY_STATE_KEY="engine-02-deploy/terraform.tfstate"

state_object_status() {
  local key="$1" error_file rc
  error_file="$(mktemp)"
  set +e
  aws s3api head-object --bucket "$STATE_BUCKET" --key "$key" >/dev/null 2>"$error_file"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    rm -f "$error_file"
    printf 'present'
    return 0
  fi
  if grep -Eq '(404|Not Found|NoSuchKey)' "$error_file"; then
    rm -f "$error_file"
    printf 'absent'
    return 0
  fi
  cat "$error_file" >&2
  rm -f "$error_file"
  echo "ERROR: could not determine state object status for s3://${STATE_BUCKET}/${key}" >&2
  return 1
}

delete_checksum_row() {
  local key="$1" lock_id key_json
  lock_id="${STATE_BUCKET}/${key}-md5"
  key_json="$(python3 - "$lock_id" <<'PY'
import json
import sys
print(json.dumps({"LockID": {"S": sys.argv[1]}}))
PY
)"
  aws dynamodb delete-item \
    --table-name "$LOCK_TABLE" \
    --key "$key_json" \
    --condition-expression 'attribute_not_exists(Info)' >/dev/null
}

canonical_status="$(state_object_status "$CANONICAL_STATE_KEY")"
legacy_status="$(state_object_status "$LEGACY_STATE_KEY")"
if [ "$canonical_status" = absent ] && [ "$legacy_status" = present ]; then
  echo "migrating legacy engine Terraform state to ${CANONICAL_STATE_KEY}"
  "${ROOT_DIR}/scripts/generate_backend.sh" "$STATE_BUCKET" engine-02-deploy "$REGION" "$DEPLOY_DIR" "$LOCK_TABLE"
  terraform -chdir="$DEPLOY_DIR" init -reconfigure -input=false
  "${ROOT_DIR}/scripts/generate_backend.sh" "$STATE_BUCKET" engine "$REGION" "$DEPLOY_DIR" "$LOCK_TABLE"
  delete_checksum_row "$CANONICAL_STATE_KEY"
  terraform -chdir="$DEPLOY_DIR" init -migrate-state -force-copy -input=false
  migrated_resources="$(terraform -chdir="$DEPLOY_DIR" state list)"
  [ -n "$migrated_resources" ] || {
    echo "ERROR: migrated canonical engine state is empty; preserving legacy state" >&2
    exit 1
  }
  aws s3api delete-object --bucket "$STATE_BUCKET" --key "$LEGACY_STATE_KEY" >/dev/null
  delete_checksum_row "$LEGACY_STATE_KEY"
elif [ "$canonical_status" = present ] && [ "$legacy_status" = present ]; then
  echo "ERROR: both canonical and legacy engine state objects exist; refusing an ambiguous migration" >&2
  exit 1
else
  "${ROOT_DIR}/scripts/generate_backend.sh" "$STATE_BUCKET" engine "$REGION" "$DEPLOY_DIR" "$LOCK_TABLE"
  terraform -chdir="$DEPLOY_DIR" init -reconfigure -input=false
fi
(
  cd "$DEPLOY_DIR"
  "${ENGINE_ROOT}/scripts/generate_tfvars.sh"
  terraform apply -input=false -auto-approve
)
"${ROOT_DIR}/scripts/upload_source.sh" "$STATE_BUCKET" engine "$ENGINE_ROOT" infra/02-deploy

echo "engine install complete (project_prefix=${PROJECT_PREFIX})"
