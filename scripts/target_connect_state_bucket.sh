#!/usr/bin/env bash
set -euo pipefail

# Resolve the S3 backend bucket for target-connect.
# Uses target_state_bucket_arn from install SSM when configured; otherwise the
# conventional ${OPENCI_TF_PROJECT}-state-<caller-account-id> name.
#
# Usage: target_connect_state_bucket.sh

PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCT" =~ ^[0-9]{12}$ ]] || {
  echo "ERROR: invalid caller account id: $ACCT" >&2
  exit 1
}

DEFAULT="${PROJECT}-state-${ACCT}"
TARGET_STATE_ARN="$(./scripts/ssm_config.sh get-or target_state_bucket_arn '')"
if [ -z "$TARGET_STATE_ARN" ]; then
  echo "$DEFAULT"
  exit 0
fi

./scripts/bucket_from_s3_arn.sh "$TARGET_STATE_ARN"
