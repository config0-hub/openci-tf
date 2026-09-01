#!/usr/bin/env bash
set -euo pipefail

# Generates a backend.tf file in the specified directory for S3 remote state.
# The backend carries bucket/key/region only (version-agnostic); state locking
# is the S3 native lock file, passed at init time as
# -backend-config=use_lockfile=true by openci-tf's own runs (tofu/terraform
# >= 1.10). No DynamoDB lock table exists.
#
# Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir>
#
# Example:
#   ./scripts/generate_backend.sh openci-tf-state-111111111111 deploy us-east-1 infra/deploy/

BUCKET_NAME="${1:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir>}"
STATE_KEY="${2:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir>}"
REGION="${3:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir>}"
TARGET_DIR="${4:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir>}"
if [ "$#" -gt 4 ]; then
  echo "ERROR: generate_backend.sh takes exactly 4 arguments; the lock-table argument was removed (S3 native lock file)" >&2
  exit 1
fi

cat >"${TARGET_DIR}/backend.tf" <<BACKEND_EOF
terraform {
  backend "s3" {
    bucket = "${BUCKET_NAME}"
    key    = "${STATE_KEY}/terraform.tfstate"
    region = "${REGION}"
  }
}
BACKEND_EOF

echo "Generated ${TARGET_DIR}/backend.tf (bucket=${BUCKET_NAME}, key=${STATE_KEY}/terraform.tfstate)"
