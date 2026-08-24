#!/usr/bin/env bash
set -euo pipefail

# Proves `ssm_config.sh set-stdin` strips exactly one trailing newline before SSM write.
# Run: tests/install/test_ssm_config_set_stdin.sh

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MOCK_DIR="$(mktemp -d)"
trap 'rm -rf "$MOCK_DIR"' EXIT

write_aws_mock() {
  cat >"${MOCK_DIR}/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "ssm" && "${2:-}" == "put-parameter" && "${3:-}" == "--cli-input-json" ]]; then
  json_file="${4#file://}"
  printf '%s' "$(<"$json_file")" >"${MOCK_PUT_JSON:?}"
  exit 0
fi
echo "unexpected aws call: $*" >&2
exit 97
EOF
  chmod +x "${MOCK_DIR}/aws"
}

assert_json_value() {
  local label="$1"
  local want="$2"
  local got
  got="$(jq -r '.Value' "${MOCK_PUT_JSON:?}")"
  if [[ "$got" != "$want" ]]; then
    echo "FAIL: ${label} expected Value=${want@Q} got ${got@Q}" >&2
    exit 1
  fi
  echo "PASS: ${label}"
}

write_aws_mock
export MOCK_PUT_JSON="${MOCK_DIR}/put-parameter.json"
export PATH="${MOCK_DIR}:${PATH}"
export SSM_CONFIG_PROJECT="test-ssm-config-stdin"

printf 'secret-value\n' | "${ROOT}/scripts/ssm_config.sh" set-stdin webhook_secret
assert_json_value "strips one trailing newline" "secret-value"

printf 'secret-with-two-lines\nsecond-line\n' | "${ROOT}/scripts/ssm_config.sh" set-stdin webhook_secret
assert_json_value "keeps internal newline" $'secret-with-two-lines\nsecond-line'

if printf '\n' | "${ROOT}/scripts/ssm_config.sh" set-stdin webhook_secret 2>/dev/null; then
  echo "FAIL: newline-only stdin should fail" >&2
  exit 1
fi
echo "PASS: newline-only stdin rejected"

if printf '' | "${ROOT}/scripts/ssm_config.sh" set-stdin webhook_secret 2>/dev/null; then
  echo "FAIL: empty stdin should fail" >&2
  exit 1
fi
echo "PASS: empty stdin rejected"
