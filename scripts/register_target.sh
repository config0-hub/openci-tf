#!/usr/bin/env bash
# Hub-side target registration: alias row, target_account_ids append, deploy.
set -euo pipefail

PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
ALIAS=""
TARGET_ACCOUNT_ID=""

usage() {
  echo 'Usage: register_target.sh --alias ALIAS --account-id 12_DIGITS' >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --alias) ALIAS="$2"; shift 2 ;;
    --account-id) TARGET_ACCOUNT_ID="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

for var in ALIAS TARGET_ACCOUNT_ID; do
  [[ -n "${!var:-}" ]] || {
    echo "ERROR: --${var,,} is required" | sed 's/_/-/g' >&2
    exit 1
  }
done

[[ "$TARGET_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
  echo "ERROR: account-id must be 12 digits" >&2
  exit 1
}

HUB_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
[[ "$HUB_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || {
  echo "ERROR: could not resolve a valid hub account id from caller identity" >&2
  exit 1
}

ROLE_NAME="${PROJECT}-executor-readonly"
POWERUSER_ROLE_NAME="${PROJECT}-executor-poweruser"

./scripts/append_target_account_id.sh "$TARGET_ACCOUNT_ID"
just deploy

./scripts/register_account.sh \
  --alias "$ALIAS" \
  --account-id "$TARGET_ACCOUNT_ID" \
  --role-name "$ROLE_NAME" \
  --poweruser-role-name "$POWERUSER_ROLE_NAME"
