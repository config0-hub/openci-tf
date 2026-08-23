#!/usr/bin/env bash
# Canonical account-alias contract shared by registration, setter, and folder config.
set -euo pipefail
MAX_ACCOUNT_ALIAS_CHARS=128
alias="${1:?Usage: validate_account_alias.sh ALIAS}"
if [[ -z "${alias//[[:space:]]/}" ]]; then
  echo 'Error: invalid alias' >&2
  exit 1
fi
if ((${#alias} > MAX_ACCOUNT_ALIAS_CHARS)); then
  echo 'Error: invalid alias' >&2
  exit 1
fi
