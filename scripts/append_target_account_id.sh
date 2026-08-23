#!/usr/bin/env bash
set -euo pipefail

# Append one 12-digit account id to target_account_ids without duplicates.
#
# Usage: append_target_account_id.sh <12-digit-account-id>

ACCOUNT_ID="${1:?Usage: append_target_account_id.sh <12-digit-account-id>}"

[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
  echo "ERROR: account id must be 12 digits" >&2
  exit 1
}

command -v jq >/dev/null || {
  echo "ERROR: jq is required" >&2
  exit 1
}

CURRENT="$(./scripts/ssm_config.sh get-or target_account_ids '[]')"
if ! echo "$CURRENT" | jq -e 'type == "array"' >/dev/null 2>&1; then
  echo "ERROR: target_account_ids is not a JSON array: $CURRENT" >&2
  exit 1
fi

UPDATED="$(echo "$CURRENT" | jq --arg id "$ACCOUNT_ID" 'if index($id) then . else . + [$id] end')"
if [ "$UPDATED" = "$CURRENT" ]; then
  echo "target account $ACCOUNT_ID already listed in target_account_ids"
else
  ./scripts/ssm_config.sh set target_account_ids "$UPDATED"
  echo "appended $ACCOUNT_ID to target_account_ids"
fi
