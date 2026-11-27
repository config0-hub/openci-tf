#!/usr/bin/env bash
# Destroy engine infra/02-deploy when the engine checkout has no justfile uninstall recipe.
set -euo pipefail

ENGINE_ROOT="${1:?Usage: engine_uninstall.sh <engine_repo_path>}"
PROJECT_PREFIX="${OPENCI_TF_PROJECT:-openci-tf}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="${PROJECT_PREFIX}-state-${ACCT}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE_ROOT="$(cd "$ENGINE_ROOT" && pwd)"
DEPLOY_DIR="${ENGINE_ROOT}/infra/02-deploy"
ECR_DIR="${ENGINE_ROOT}/infra/01-ecr"

if ! command -v tofu >/dev/null 2>&1; then
  echo "ERROR: tofu is required but not found in PATH" >&2
  exit 1
fi

if [ ! -d "$DEPLOY_DIR" ]; then
  echo "ERROR: engine checkout missing infra/02-deploy: ${ENGINE_ROOT}" >&2
  exit 1
fi

state_file="$(mktemp)"
err_file="$(mktemp)"
trap 'rm -f "$state_file" "$err_file"' EXIT

# engine_install.sh writes terraform.tfvars into this checkout. When it is
# missing, the engine state tells the two cases apart. No managed resources in
# state (no state object, or one a destroy emptied) means the engine is not
# installed: there is nothing to destroy. Resources in state mean the engine is
# installed and only the file is gone: skipping would hide a destroy that did
# not run, so stop with what to do. An unreadable state also stops.
require_no_engine_resources() {
  local dir="$1" key="$2/terraform.tfstate" rc count
  set +e
  aws s3api get-object --bucket "$STATE_BUCKET" --key "$key" "$state_file" >/dev/null 2>"$err_file"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    count="$(jq '[.resources[]? | select(.mode == "managed")] | length' "$state_file")"
  elif grep -Eq 'NoSuchKey|NoSuchBucket' "$err_file"; then
    count=0
  else
    cat "$err_file" >&2
    echo "ERROR: could not read engine state s3://${STATE_BUCKET}/${key}" >&2
    exit "$rc"
  fi
  if [ "$count" -ne 0 ]; then
    echo "ERROR: ${dir}/terraform.tfvars is missing, but s3://${STATE_BUCKET}/${key} still tracks ${count} engine resource(s); the engine was NOT destroyed." >&2
    echo "Run this from the engine checkout that installed it (ENGINE_REPO_PATH), or run 'just engine' to write terraform.tfvars again, then run 'just engine-destroy'." >&2
    exit 1
  fi
  echo "no ${dir}/terraform.tfvars and no resources in s3://${STATE_BUCKET}/${key}: the engine is not installed, nothing to destroy"
}

"${ROOT_DIR}/scripts/generate_backend.sh" "$STATE_BUCKET" engine "$REGION" "$DEPLOY_DIR"
(
  cd "$DEPLOY_DIR"
  if [ -f terraform.tfvars ]; then
    terraform init -reconfigure -input=false -backend-config=use_lockfile=true
    terraform destroy -input=false -auto-approve
  else
    require_no_engine_resources "$DEPLOY_DIR" engine
  fi
)

if [ ! -d "$ECR_DIR" ]; then
  echo "ERROR: engine checkout missing infra/01-ecr: ${ENGINE_ROOT}" >&2
  exit 1
fi

"${ROOT_DIR}/scripts/generate_backend.sh" "$STATE_BUCKET" engine-ecr "$REGION" "$ECR_DIR"
(
  cd "$ECR_DIR"
  if [ -f terraform.tfvars ]; then
    tofu init -reconfigure -input=false -backend-config=use_lockfile=true
    tofu destroy -input=false -auto-approve
  else
    require_no_engine_resources "$ECR_DIR" engine-ecr
  fi
)

if [ ! -f "${DEPLOY_DIR}/terraform.tfvars" ] && [ ! -f "${ECR_DIR}/terraform.tfvars" ]; then
  echo "engine uninstall: the engine is not installed; nothing was destroyed"
else
  echo "engine uninstall complete"
fi
