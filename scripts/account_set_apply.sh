#!/usr/bin/env bash
# Update enable_apply on an existing account alias registration.
set -euo pipefail
REGION=us-east-1; TABLE=openci-tf-settings
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) echo 'Usage: account_set_apply.sh --alias ALIAS --enable-apply true|false [--region REGION] [--table TABLE]'; exit 0 ;;
    --alias) ALIAS="$2"; shift 2 ;;
    --enable-apply) ENABLE_APPLY="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --table) TABLE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done
for var in ALIAS ENABLE_APPLY; do [[ -n "${!var:-}" ]] || { echo "Error: --${var,,} is required" | sed 's/_/-/g'; exit 1; }; done
case "$ENABLE_APPLY" in
  true|false) ;;
  *) echo 'Error: --enable-apply must be true or false' >&2; exit 1 ;;
esac
"$(dirname "$0")/validate_account_alias.sh" "$ALIAS"
KEY_JSON="$(python3 - "$ALIAS" <<'PY'
import json
import sys

alias = sys.argv[1]
print(json.dumps({"pk": {"S": "account"}, "sk": {"S": alias}}, separators=(",", ":")))
PY
)"
ATTR_VALUES="$(python3 - "$ENABLE_APPLY" <<'PY'
import json
import sys

enable_apply = sys.argv[1]
print(json.dumps({":val": {"BOOL": enable_apply == "true"}}, separators=(",", ":")))
PY
)"
aws dynamodb update-item \
  --region "$REGION" \
  --table-name "$TABLE" \
  --key "$KEY_JSON" \
  --update-expression "SET enable_apply = :val" \
  --condition-expression "attribute_exists(pk)" \
  --expression-attribute-values "$ATTR_VALUES"
echo "Set enable_apply=$ENABLE_APPLY for account alias $ALIAS"
