#!/usr/bin/env bash
# Store the Infracost API key in SSM (SecureString). Value is read from stdin only.
set -euo pipefail

SSM_PATH="/openci-tf/infracost/api_key"
value="$(cat)"
if [ -z "$value" ]; then
  echo "ERROR: empty value on stdin" >&2
  exit 1
fi

aws ssm put-parameter --name "$SSM_PATH" --value "$value" \
  --type SecureString --overwrite >/dev/null
echo "configured ${SSM_PATH}"
