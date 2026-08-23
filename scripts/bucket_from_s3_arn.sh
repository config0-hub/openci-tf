#!/usr/bin/env bash
set -euo pipefail

# Extract the bucket name from an S3 bucket ARN.
#
# Usage: bucket_from_s3_arn.sh <arn:aws:s3:::bucket-name[/key]>

ARN="${1:?Usage: bucket_from_s3_arn.sh <s3-bucket-arn>}"

if [[ ! "$ARN" =~ ^arn:aws:s3:::([^/]+) ]]; then
  echo "ERROR: invalid S3 bucket ARN: $ARN" >&2
  exit 1
fi

echo "${BASH_REMATCH[1]}"
