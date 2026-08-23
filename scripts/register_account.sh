#!/usr/bin/env bash
# Register one target account alias used by folder configuration.
set -euo pipefail
REGION=us-east-1; TABLE=openci-tf-settings; ROLE_NAME="${OPENCI_TF_ROLE_NAME:-openci-tf-executor-readonly}"
POWERUSER_ROLE_NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) echo 'Usage: register_account.sh --alias ALIAS --account-id 12_DIGITS [--role-name NAME] [--poweruser-role-name NAME] [--max-ttl SECONDS] [--enable-apply true|false] [--region REGION] [--table TABLE]'; exit 0 ;;
    --alias) ALIAS="$2"; shift 2 ;; --account-id) ACCOUNT_ID="$2"; shift 2 ;;
    --role-name) ROLE_NAME="$2"; shift 2 ;;
    --poweruser-role-name) POWERUSER_ROLE_NAME="$2"; shift 2 ;;
    --max-ttl) MAX_TTL="$2"; shift 2 ;; --enable-apply) ENABLE_APPLY="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;; --table) TABLE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done
for var in ALIAS ACCOUNT_ID; do [[ -n "${!var:-}" ]] || { echo "Error: --${var,,} is required" | sed 's/_/-/g'; exit 1; }; done
"$(dirname "$0")/validate_account_alias.sh" "$ALIAS"
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo 'Error: account-id must be 12 digits' >&2; exit 1; }
[[ "$ROLE_NAME" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]] || { echo 'Error: invalid role-name' >&2; exit 1; }
if [[ -n "$POWERUSER_ROLE_NAME" ]]; then
  [[ "$POWERUSER_ROLE_NAME" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]] || { echo 'Error: invalid poweruser-role-name' >&2; exit 1; }
fi
MAX_TTL_INT=""
if [[ -n "${MAX_TTL:-}" ]]; then
  [[ "$MAX_TTL" =~ ^[0-9]+$ ]] || { echo 'Error: max-ttl must be an integer >= 900' >&2; exit 1; }
  MAX_TTL_INT="$((10#$MAX_TTL))"
  [[ "$MAX_TTL_INT" -ge 900 ]] || { echo 'Error: max-ttl must be an integer >= 900' >&2; exit 1; }
fi
ENABLE_APPLY="${ENABLE_APPLY:-false}"
case "$ENABLE_APPLY" in
  true|false) ;;
  *) echo 'Error: --enable-apply must be true or false' >&2; exit 1 ;;
esac
if [[ "$ENABLE_APPLY" == "true" && -z "$POWERUSER_ROLE_NAME" ]]; then
  POWERUSER_ROLE_NAME="${OPENCI_TF_PROJECT:-openci-tf}-executor-poweruser"
fi
HUB_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
[[ "$HUB_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || { echo 'Error: could not resolve a valid hub account id from caller identity' >&2; exit 1; }
EXTERNAL_ID="$(./scripts/derive_external_id.sh "$HUB_ACCOUNT_ID" "$ACCOUNT_ID")"
item="$(python3 - "$ALIAS" "$ACCOUNT_ID" "$ROLE_NAME" "$POWERUSER_ROLE_NAME" "$EXTERNAL_ID" "$MAX_TTL_INT" "$ENABLE_APPLY" <<'PY'
import json
import sys

alias, account_id, role_name, poweruser_role_name, external_id, max_ttl, enable_apply = sys.argv[1:]
item = {
    "pk": {"S": "account"},
    "sk": {"S": alias},
    "account_id": {"S": account_id},
    "role_name": {"S": role_name},
    "external_id": {"S": external_id},
    "enable_apply": {"BOOL": enable_apply == "true"},
}
if poweruser_role_name:
    item["poweruser_role_name"] = {"S": poweruser_role_name}
if max_ttl:
    item["max_ttl"] = {"N": str(int(max_ttl))}
print(json.dumps(item, separators=(",", ":")))
PY
)"
aws dynamodb put-item --region "$REGION" --table-name "$TABLE" --item "$item"
echo "Registered account alias $ALIAS ($ACCOUNT_ID) with readonly role $ROLE_NAME and derived ExternalId"
if [[ -n "$POWERUSER_ROLE_NAME" ]]; then
  echo "Poweruser role name: $POWERUSER_ROLE_NAME"
fi
echo "Reminder: add $ACCOUNT_ID to target_account_ids and re-run just deploy before using this account."
