#!/usr/bin/env bash
set -euo pipefail

# Remove infra/bootstrap/.terraform when it caches a DIFFERENT account's S3
# backend bucket than the one this run targets. Terraform init -reconfigure
# cannot unset a prior s3 backend; deleting the local metadata cache is safe
# (remote S3 state is untouched).
#
# Usage: clear_stale_bootstrap_backend_cache.sh <expected_bucket>

BUCKET="${1:?Usage: clear_stale_bootstrap_backend_cache.sh <expected_bucket>}"
CACHE="infra/bootstrap/.terraform/terraform.tfstate"

if [ ! -f "$CACHE" ]; then
  exit 0
fi

CACHED_BUCKET=""
if CACHED_BUCKET="$(jq -er '.backend.config.bucket' "$CACHE" 2>/dev/null)"; then
  :
else
  exit 0
fi

if [ -z "$CACHED_BUCKET" ] || [ "$CACHED_BUCKET" = "null" ]; then
  exit 0
fi

if [ "$CACHED_BUCKET" = "$BUCKET" ]; then
  exit 0
fi

echo "removing stale bootstrap .terraform cache (cached backend bucket ${CACHED_BUCKET} != run bucket ${BUCKET})"
rm -rf infra/bootstrap/.terraform
