#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MOCK_DIR="$(mktemp -d)"
trap 'rm -rf "$MOCK_DIR"' EXIT

expect_rc() {
  local label="$1"
  local want="$2"
  shift 2
  set +e
  "$@"
  local got=$?
  set -e
  if [[ "$got" -ne "$want" ]]; then
    echo "FAIL: ${label} expected rc=${want} got rc=${got}" >&2
    exit 1
  fi
  echo "PASS: ${label}"
}

write_aws_put_parameter_mock() {
  cat >"${MOCK_DIR}/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" != "ssm" || "${2:-}" != "put-parameter" ]]; then
  echo "unexpected aws call: $*" >&2
  exit 97
fi
printf '%s\n' "$*" >"${MOCK_AWS_ARGV:?}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) printf '%s' "$2" >"${MOCK_SSM_NAME:?}"; shift 2 ;;
    --value) value="$2"; shift 2 ;;
    --type) printf '%s' "$2" >"${MOCK_SSM_TYPE:?}"; shift 2 ;;
    *) shift ;;
  esac
done
: "${value:?missing --value}"
printf '%s' "$value" >"${MOCK_VALUE_ARG:?}"
case "$value" in
  file://*) ;;
  *) echo "value is not a file:// argument" >&2; exit 98 ;;
esac
path="${value#file://}"
printf '%s' "$path" >"${MOCK_VALUE_PATH:?}"
if [[ ! -r "$path" ]]; then
  echo "value file is not readable" >&2
  exit 99
fi
if mode="$(stat -f %Lp "$path" 2>/dev/null)"; then
  :
else
  mode="$(stat -c %a "$path")"
fi
printf '%s' "$mode" >"${MOCK_VALUE_MODE:?}"
printf '%s' "$(cat "$path")" >"${MOCK_VALUE_CONTENT:?}"
exit 0
EOF
  chmod +x "${MOCK_DIR}/aws"
}

assert_token_absent_from_argv_and_output() {
  local token="$1"
  for file in "$MOCK_AWS_ARGV" "${MOCK_DIR}/stdout" "${MOCK_DIR}/stderr"; do
    if grep -q -- "$token" "$file"; then
      echo "FAIL: raw token appeared in ${file}" >&2
      exit 1
    fi
  done
}

MOCK_AWS_ARGV="${MOCK_DIR}/aws.argv"
MOCK_SSM_NAME="${MOCK_DIR}/ssm.name"
MOCK_SSM_TYPE="${MOCK_DIR}/ssm.type"
MOCK_VALUE_ARG="${MOCK_DIR}/value.arg"
MOCK_VALUE_PATH="${MOCK_DIR}/value.path"
MOCK_VALUE_MODE="${MOCK_DIR}/value.mode"
MOCK_VALUE_CONTENT="${MOCK_DIR}/value.content"
write_aws_put_parameter_mock

STDIN_TOKEN='github_pat_dummy_install_stdin_token'
printf '%s' "$STDIN_TOKEN" | env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" \
  MOCK_AWS_ARGV="$MOCK_AWS_ARGV" MOCK_SSM_NAME="$MOCK_SSM_NAME" MOCK_SSM_TYPE="$MOCK_SSM_TYPE" \
  MOCK_VALUE_ARG="$MOCK_VALUE_ARG" MOCK_VALUE_PATH="$MOCK_VALUE_PATH" MOCK_VALUE_MODE="$MOCK_VALUE_MODE" \
  MOCK_VALUE_CONTENT="$MOCK_VALUE_CONTENT" \
  "${ROOT}/scripts/install_github_control_token.sh" --repo org/repo --token-file - \
  >"${MOCK_DIR}/stdout" 2>"${MOCK_DIR}/stderr"

echo "PASS: install accepts token on stdin"
[[ "$(<"$MOCK_SSM_NAME")" == "/openci-tf/clone-token/org-repo-control" ]]
[[ "$(<"$MOCK_SSM_TYPE")" == "SecureString" ]]
[[ "$(<"$MOCK_VALUE_ARG")" == file://* ]]
[[ "$(<"$MOCK_VALUE_CONTENT")" == "$STDIN_TOKEN" ]]
[[ "$(<"$MOCK_VALUE_MODE")" == "600" ]]
stdin_temp_path="$(<"$MOCK_VALUE_PATH")"
if [[ -e "$stdin_temp_path" ]]; then
  echo "FAIL: stdin token temp file was not cleaned up" >&2
  exit 1
fi
assert_token_absent_from_argv_and_output "$STDIN_TOKEN"

TOKEN_FILE="${MOCK_DIR}/named-token.txt"
FILE_TOKEN='github_pat_dummy_install_file_token'
printf '%s' "$FILE_TOKEN" >"$TOKEN_FILE"
: >"${MOCK_DIR}/stdout"
: >"${MOCK_DIR}/stderr"
expect_rc "install accepts token file" 0 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" \
  MOCK_AWS_ARGV="$MOCK_AWS_ARGV" MOCK_SSM_NAME="$MOCK_SSM_NAME" MOCK_SSM_TYPE="$MOCK_SSM_TYPE" \
  MOCK_VALUE_ARG="$MOCK_VALUE_ARG" MOCK_VALUE_PATH="$MOCK_VALUE_PATH" MOCK_VALUE_MODE="$MOCK_VALUE_MODE" \
  MOCK_VALUE_CONTENT="$MOCK_VALUE_CONTENT" \
  "${ROOT}/scripts/install_github_control_token.sh" --repo org/repo --token-file "$TOKEN_FILE" \
  >"${MOCK_DIR}/stdout" 2>"${MOCK_DIR}/stderr"

[[ "$(<"$MOCK_VALUE_ARG")" == "file://${TOKEN_FILE}" ]]
[[ "$(<"$MOCK_VALUE_CONTENT")" == "$FILE_TOKEN" ]]
assert_token_absent_from_argv_and_output "$FILE_TOKEN"
