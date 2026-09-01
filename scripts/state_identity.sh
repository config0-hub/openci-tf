#!/usr/bin/env bash
set -euo pipefail

# Validate that a local bootstrap Terraform state file is EXACTLY the openci-tf
# bootstrap stack's state — by managed resource ADDRESS (mode + type + name),
# not just physical attributes:
#   - exactly one managed aws_s3_bucket at address "state", tracking <bucket>
#   - a managed aws_dynamodb_table, if present, must be at address "locks"
#     and track <legacy_lock_table> (legacy pre-S3-lockfile installs;
#     apply/destroy removes it)
#   - every managed resource must be root-module and on the bootstrap-stack
#     allowlist (bucket, versioning, sse, public-access-block, legacy lock
#     table) — any unrelated managed resource, duplicate, or module resource
#     is rejected
#   - data resources are ignored but can never satisfy the bucket requirement
#
#   exit 0 -> state is exactly the expected bootstrap stack
#   exit 1 -> mismatch; callers must refuse apply/empty/destroy
#
# Usage: state_identity.sh <tfstate> <expected_bucket> <legacy_lock_table>

STATE="${1:?Usage: state_identity.sh <tfstate> <bucket> <legacy_lock_table>}"
BUCKET="${2:?expected bucket required}"
LEGACY_LOCK_TABLE="${3:?expected legacy lock table required}"

if ! jq -e --arg b "$BUCKET" --arg t "$LEGACY_LOCK_TABLE" '
  [.resources[]? | select(.mode == "managed")] as $managed
  | ($managed | map(select(.type == "aws_s3_bucket"))) as $buckets
  | ($managed | map(select(.type == "aws_dynamodb_table"))) as $tables
  # exactly one managed bucket, at address "state", with the expected name
  | ($buckets | length == 1)
  and ($buckets[0].name == "state")
  and ([$buckets[0].instances[].attributes.bucket] | (length > 0) and all(. == $b))
  # legacy managed lock table optional; when present it must have the exact
  # address and physical name that this bootstrap stack formerly owned
  and ($tables | length <= 1)
  and (($tables | length == 0)
       or (($tables[0].name == "locks")
           and ([$tables[0].instances[].attributes.name] | (length > 0) and all(. == $t))))
  # every managed resource is root-module and on the bootstrap allowlist
  and ($managed | all(
        ((.module // "") == "")
        and ([.type, .name] as $addr
             | $addr == ["aws_s3_bucket", "state"]
            or $addr == ["aws_s3_bucket_versioning", "state"]
            or $addr == ["aws_s3_bucket_server_side_encryption_configuration", "state"]
            or $addr == ["aws_s3_bucket_public_access_block", "state"]
            or $addr == ["aws_dynamodb_table", "locks"])))
  # at most one resource entry per allowlisted address (no duplicates)
  and ($managed | group_by([.type, .name]) | all(length == 1))
  # every present S3 child resource must be physically bound to OUR bucket —
  # an allowlisted address pointing at a foreign bucket is rejected
  and ($managed
       | map(select(.type == "aws_s3_bucket_versioning"
                 or .type == "aws_s3_bucket_server_side_encryption_configuration"
                 or .type == "aws_s3_bucket_public_access_block"))
       | all([.instances[].attributes.bucket] | (length > 0) and all(. == $b)))
' "$STATE" >/dev/null; then
  echo "ERROR: local state ${STATE} is not exactly the openci-tf bootstrap stack for ${BUCKET}/${LEGACY_LOCK_TABLE}; refusing to use it" >&2
  exit 1
fi
