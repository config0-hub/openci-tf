#!/usr/bin/env bash
# Target-account onboarding: verify identity, backend state bucket, SSM tfvars, target-connect.
# No per-target state bucket is created and no lock table exists; state locking
# is the S3 native lock file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=phase_timing.sh
source "$SCRIPT_DIR/phase_timing.sh"

PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
HUB_ACCOUNT_ID=""
STATE_BUCKET=""
STATE_BUCKET_ARG=""

usage() {
  echo 'Usage: target_onboard.sh --hub-account-id 12_DIGITS [--state-bucket NAME]' >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --hub-account-id) HUB_ACCOUNT_ID="$2"; shift 2 ;;
    --state-bucket) STATE_BUCKET="$2"; STATE_BUCKET_ARG=1; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$HUB_ACCOUNT_ID" ]] || {
  echo "ERROR: --hub-account-id is required" >&2
  exit 1
}
[[ "$HUB_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
  echo "ERROR: hub-account-id must be 12 digits" >&2
  exit 1
}

TARGET_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
[[ "$TARGET_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
  echo "ERROR: could not resolve a valid target account id from caller identity" >&2
  exit 1
}

if [ -z "$STATE_BUCKET" ]; then
  STATE_BUCKET="${PROJECT}-state-${TARGET_ACCOUNT_ID}"
fi

verify_prerequisites() {
set +e
./scripts/bucket_exists.sh "$STATE_BUCKET"
probe_rc=$?
set -e
case "$probe_rc" in
  0) ;;
  1)
    echo "ERROR: state bucket ${STATE_BUCKET} does not exist in account ${TARGET_ACCOUNT_ID}" >&2
    echo "ERROR: openci-tf does not create per-target state buckets; pass --state-bucket NAME to use an existing (shared) bucket" >&2
    exit 1
    ;;
  *)
    exit "$probe_rc"
    ;;
esac
}
phase_timing_run verify-prerequisites verify_prerequisites

HUB_ROLE_ARN="arn:aws:iam::${HUB_ACCOUNT_ID}:role/${PROJECT}-hub-lambda-exec"
TARGET_STATE_ARN="arn:aws:s3:::${STATE_BUCKET}"

onboard_ssm_config() {
./scripts/ssm_config.sh set hub_lambda_exec_role_arn "$HUB_ROLE_ARN"
./scripts/ssm_config.sh set target_state_bucket_arn "$TARGET_STATE_ARN"
}
phase_timing_run ssm-config onboard_ssm_config

echo "target onboard: account=${TARGET_ACCOUNT_ID} bucket=${STATE_BUCKET} hub=${HUB_ACCOUNT_ID}"
onboard_readonly_role() {
if [ -n "${STATE_BUCKET_ARG:-}" ]; then
  just target-create-aws-readonly "$HUB_ACCOUNT_ID" "$STATE_BUCKET"
else
  just target-create-aws-readonly "$HUB_ACCOUNT_ID"
fi
}
phase_timing_run target-create-aws-readonly onboard_readonly_role
