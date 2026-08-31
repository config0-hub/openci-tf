#!/usr/bin/env bash
set -euo pipefail

# Report a Terraform S3 native lock file and fail with the exact force-unlock
# command. Does not force-unlock or delete the lock object: the script cannot
# know whether a deploy is running.
#
# Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key>

TF_DIR="${1:?Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key>}"
STATE_BUCKET="${2:?Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key>}"
STATE_KEY="${3:?Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key>}"

LOCK_OBJECT_KEY="${STATE_KEY}/terraform.tfstate.tflock"

err_file="$(mktemp)"
set +e
LOCK_BODY="$(aws s3api get-object --bucket "$STATE_BUCKET" --key "$LOCK_OBJECT_KEY" /dev/stdout 2>"$err_file")"
get_rc=$?
set -e
if [ "$get_rc" -ne 0 ]; then
  if grep -Eq 'NoSuchKey|Not Found|404' "$err_file"; then
    rm -f "$err_file"
    exit 0
  fi
  cat "$err_file" >&2
  rm -f "$err_file"
  echo "ERROR: could not determine lock status for s3://${STATE_BUCKET}/${LOCK_OBJECT_KEY}" >&2
  exit "$get_rc"
fi
rm -f "$err_file"

LOCK_UUID="$(printf '%s' "$LOCK_BODY" | jq -r '.ID // empty' 2>/dev/null || true)"
CREATED="$(printf '%s' "$LOCK_BODY" | jq -r '.Created // empty' 2>/dev/null || true)"
WHO="$(printf '%s' "$LOCK_BODY" | jq -r '.Who // "unknown"' 2>/dev/null || echo unknown)"
OPERATION="$(printf '%s' "$LOCK_BODY" | jq -r '.Operation // "unknown"' 2>/dev/null || echo unknown)"

if [ -z "$LOCK_UUID" ] || [ -z "$CREATED" ]; then
  echo "ERROR: lock object s3://${STATE_BUCKET}/${LOCK_OBJECT_KEY} exists but has no readable ID or Created; refusing to guess" >&2
  echo "Inspect the lock object, then run:" >&2
  echo "  terraform -chdir=${TF_DIR} force-unlock <lock-id>" >&2
  exit 1
fi

AGE_MINUTES="$(python3 - "$CREATED" <<'PY'
import sys
from datetime import datetime, timezone

created = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
if created.tzinfo is None:
    created = created.replace(tzinfo=timezone.utc)
age = datetime.now(timezone.utc) - created
print(int(age.total_seconds()) // 60)
PY
)"

echo "ERROR: terraform state lock ${LOCK_UUID} is present at s3://${STATE_BUCKET}/${LOCK_OBJECT_KEY}" >&2
echo "Lock holder: ${WHO} (${OPERATION}, created ${CREATED}, age ${AGE_MINUTES} minute(s))" >&2
echo "If you are certain no deploy is using this lock, run:" >&2
echo "  terraform -chdir=${TF_DIR} force-unlock ${LOCK_UUID}" >&2
exit 1
