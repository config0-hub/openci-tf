#!/usr/bin/env bash
# Install a local dotenv file as a hub SSM SecureString under /openci-tf/env/.
# Secret content is read from the file and sent via a mode-0600 CLI input JSON file.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

SSM_PATH="${1:?Usage: install_ssm_env.sh <ssm_path> <dotenv_file>}"
DOTENV_FILE="${2:?Usage: install_ssm_env.sh <ssm_path> <dotenv_file>}"
KMS_ALIAS="${KMS_ALIAS:-alias/openci-tf-foundation}"
PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
if [ "${KMS_ALIAS}" = "alias/openci-tf-foundation" ] && [ "${PROJECT}" != "openci-tf" ]; then
  KMS_ALIAS="alias/${PROJECT}-foundation"
fi
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

if [ ! -f "$DOTENV_FILE" ]; then
  echo "ERROR: dotenv file not found: ${DOTENV_FILE}" >&2
  exit 1
fi

request_file="$(mktemp)"
chmod 600 "$request_file"
trap 'rm -f "$request_file"' EXIT

python3 - "$SSM_PATH" "$DOTENV_FILE" "$KMS_ALIAS" "$request_file" <<'PY'
import json
import sys

from src.domain.ssm_env.dotenv import parse_dotenv
from src.domain.ssm_env.paths import validate_ssm_env_path

path = validate_ssm_env_path(sys.argv[1])
with open(sys.argv[2], encoding="utf-8") as handle:
    content = handle.read()
parse_dotenv(content, source=path)
payload = {
    "Name": path,
    "Value": content,
    "Type": "SecureString",
    "Overwrite": True,
    "KeyId": sys.argv[3],
}
with open(sys.argv[4], "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
PY

aws ssm put-parameter --region "$REGION" --cli-input-json "file://${request_file}" >/dev/null
echo "configured ${SSM_PATH}"
