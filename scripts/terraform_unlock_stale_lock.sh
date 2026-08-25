#!/usr/bin/env bash
set -euo pipefail

# Report a Terraform state lock and fail with the exact force-unlock command.
# Does not force-unlock: the script cannot know whether a deploy is running.
#
# Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key> <lock_table>

TF_DIR="${1:?Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key> <lock_table>}"
STATE_BUCKET="${2:?Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key> <lock_table>}"
STATE_KEY="${3:?Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key> <lock_table>}"
LOCK_TABLE="${4:?Usage: terraform_unlock_stale_lock.sh <terraform_dir> <state_bucket> <state_key> <lock_table>}"

LOCK_ID="${STATE_BUCKET}/${STATE_KEY}/terraform.tfstate"
KEY_JSON="$(python3 - "$LOCK_ID" <<'PY'
import json
import sys
print(json.dumps({"LockID": {"S": sys.argv[1]}}))
PY
)"

ITEM="$(aws dynamodb get-item --table-name "$LOCK_TABLE" --key "$KEY_JSON" --output json)"
if [ -z "$ITEM" ] || [ "$ITEM" = "null" ] || ! echo "$ITEM" | jq -e '.Item.Info.S' >/dev/null 2>&1; then
  exit 0
fi

INFO_JSON="$(echo "$ITEM" | jq -r '.Item.Info.S')"
LOCK_UUID="$(echo "$INFO_JSON" | jq -r '.ID // empty')"
CREATED="$(echo "$INFO_JSON" | jq -r '.Created // empty')"
WHO="$(echo "$INFO_JSON" | jq -r '.Who // "unknown"')"
OPERATION="$(echo "$INFO_JSON" | jq -r '.Operation // "unknown"')"

if [ -z "$LOCK_UUID" ] || [ -z "$CREATED" ]; then
  echo "ERROR: lock item ${LOCK_ID} exists but Info is missing ID or Created; refusing to guess" >&2
  echo "Inspect the lock in DynamoDB table ${LOCK_TABLE}, then run:" >&2
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

echo "ERROR: terraform state lock ${LOCK_UUID} is present on ${LOCK_ID}" >&2
echo "Lock holder: ${WHO} (${OPERATION}, created ${CREATED}, age ${AGE_MINUTES} minute(s))" >&2
echo "If you are certain no deploy is using this lock, run:" >&2
echo "  terraform -chdir=${TF_DIR} force-unlock ${LOCK_UUID}" >&2
exit 1
