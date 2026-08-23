#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MOCK_DIR="$(mktemp -d)"
trap 'rm -rf "$MOCK_DIR"' EXIT
REAL_PYTHON="$(command -v python3)"

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

write_python_wrapper() {
  cat >"${MOCK_DIR}/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-m" && "${2:-}" == "src.platform.github.capability_verifier" ]]; then
  token="$(cat)"
  printf '%s' "$token" >"${MOCK_VERIFIER_STDIN:?}"
  printf '%s\n' "verifier" >>"${MOCK_LOG:?}"
  printf '%s\n' "$*" >"${MOCK_VERIFIER_ARGV:?}"
  if [[ "${MOCK_VERIFIER_RC:-0}" != "0" ]]; then
    echo "mock verifier failed" >&2
    exit "${MOCK_VERIFIER_RC}"
  fi
  echo "mock verifier passed"
  exit 0
fi
exec "${REAL_PYTHON:?}" "$@"
EOF
  chmod +x "${MOCK_DIR}/python3"
}

write_aws_no_call_mock() {
  cat >"${MOCK_DIR}/aws" <<'EOF'
#!/usr/bin/env bash
echo "aws should not be called: $*" >&2
exit 97
EOF
  chmod +x "${MOCK_DIR}/aws"
}

write_aws_register_mock() {
  cat >"${MOCK_DIR}/aws" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "ssm" && "${2:-}" == "get-parameter" ]]; then
  printf '%s\n' "ssm" >>"${MOCK_LOG:?}"
  printf '%s\n' "${MOCK_SSM_TOKEN:?}"
  exit 0
fi
if [[ "${1:-}" == "dynamodb" && "${2:-}" == "put-item" ]]; then
  printf '%s\n' "dynamodb" >>"${MOCK_LOG:?}"
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--item" ]]; then
      printf '%s' "$2" >"${LAST_ITEM:?}"
      exit 0
    fi
    shift
  done
  echo "put-item missing --item" >&2
  exit 97
fi
echo "unexpected aws call: $*" >&2
exit 97
EOF
  chmod +x "${MOCK_DIR}/aws"
}

COMMON_ARGS=(
  --trigger-id trigger-smoke
  --repo-name org/repo
  --git-url https://github.com/org/repo.git
  --webhook-secret-ssm /openci-tf/install/smoke/webhook_secret
  --github-token-ssm /openci-tf/clone-token/smoke-token
  --upstream-urls-json '{"terraform:1.8.5":"https://releases.hashicorp.com/terraform/1.8.5/terraform_1.8.5_linux_amd64.zip","tfsec:1.28.10":"https://github.com/aquasecurity/tfsec/releases/download/v1.28.10/tfsec_1.28.10_linux_amd64.tar.gz","infracost:0.10.39":"https://github.com/infracost/infracost/releases/download/v0.10.39/infracost-linux-amd64.tar.gz"}'
)

write_python_wrapper
write_aws_no_call_mock
expect_rc "register rejects clone-token traversal" 1 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" REAL_PYTHON="${REAL_PYTHON}" \
  "${ROOT}/scripts/register_repo.sh" "${COMMON_ARGS[@]}" --github-token-ssm /openci-tf/clone-token/../install/secret

write_aws_no_call_mock
expect_rc "register rejects JSON injection in clone-token path" 1 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" REAL_PYTHON="${REAL_PYTHON}" \
  "${ROOT}/scripts/register_repo.sh" "${COMMON_ARGS[@]}" --github-token-ssm '/openci-tf/clone-token/evil"},"pk":{"S":"tamper"}'

write_aws_no_call_mock
expect_rc "register rejects non-canonical git url" 1 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" REAL_PYTHON="${REAL_PYTHON}" \
  "${ROOT}/scripts/register_repo.sh" "${COMMON_ARGS[@]}" --git-url https://attacker.example/repo.git

write_aws_no_call_mock
BAD_UPSTREAM='{"terraform:1.8.5":"http://insecure.example/bin"}'
expect_rc "register rejects non-https upstream url" 1 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" REAL_PYTHON="${REAL_PYTHON}" \
  "${ROOT}/scripts/register_repo.sh" "${COMMON_ARGS[@]}" --upstream-urls-json "$BAD_UPSTREAM"

BAD_UPSTREAM_KEY='{"terraform":"https://releases.hashicorp.com/terraform/1.8.5/terraform_1.8.5_linux_amd64.zip"}'
expect_rc "register rejects bare upstream url key" 1 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" REAL_PYTHON="${REAL_PYTHON}" \
  "${ROOT}/scripts/register_repo.sh" "${COMMON_ARGS[@]}" --upstream-urls-json "$BAD_UPSTREAM_KEY"

LAST_ITEM="${MOCK_DIR}/last-item.json"
MOCK_LOG="${MOCK_DIR}/order.log"
MOCK_VERIFIER_STDIN="${MOCK_DIR}/verifier.stdin"
MOCK_VERIFIER_ARGV="${MOCK_DIR}/verifier.argv"
MOCK_SSM_TOKEN='github_pat_dummy_control_token'
: >"$MOCK_LOG"
write_aws_register_mock
expect_rc "register verifies SSM token before DynamoDB put-item" 0 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" \
  REAL_PYTHON="${REAL_PYTHON}" MOCK_LOG="${MOCK_LOG}" MOCK_VERIFIER_STDIN="${MOCK_VERIFIER_STDIN}" \
  MOCK_VERIFIER_ARGV="${MOCK_VERIFIER_ARGV}" MOCK_SSM_TOKEN="${MOCK_SSM_TOKEN}" LAST_ITEM="${LAST_ITEM}" \
  "${ROOT}/scripts/register_repo.sh" "${COMMON_ARGS[@]}" --github-capability-collaborator known-user

printf 'ssm\nverifier\ndynamodb\n' >"${MOCK_DIR}/expected-order.log"
diff -u "${MOCK_DIR}/expected-order.log" "$MOCK_LOG"
[[ "$(<"$MOCK_VERIFIER_STDIN")" == "$MOCK_SSM_TOKEN" ]]
if grep -q -- "$MOCK_SSM_TOKEN" "$MOCK_VERIFIER_ARGV"; then
  echo "FAIL: token appeared in verifier argv" >&2
  exit 1
fi
if ! grep -q -- '--github-capability-collaborator known-user' "$MOCK_VERIFIER_ARGV"; then
  echo "FAIL: collaborator argument did not reach verifier" >&2
  exit 1
fi

python3 - <<PY "${LAST_ITEM}"
import json, sys
item = json.load(open(sys.argv[1]))
assert item["git_url"]["S"] == "https://github.com/org/repo.git"
assert item["ssm_openci_tf_github_token"]["S"] == "/openci-tf/clone-token/smoke-token"
assert item["upstream_urls"]["M"]["terraform:1.8.5"]["S"].startswith("https://")
assert "tamper" not in json.dumps(item)
print("PASS: register item encoding")
PY

: >"$MOCK_LOG"
rm -f "$LAST_ITEM"
expect_rc "verifier failure prevents repository registration" 1 env PATH="${MOCK_DIR}:${PATH}" PYTHONPATH="${ROOT}" \
  REAL_PYTHON="${REAL_PYTHON}" MOCK_LOG="${MOCK_LOG}" MOCK_VERIFIER_STDIN="${MOCK_VERIFIER_STDIN}" \
  MOCK_VERIFIER_ARGV="${MOCK_VERIFIER_ARGV}" MOCK_SSM_TOKEN="${MOCK_SSM_TOKEN}" MOCK_VERIFIER_RC=42 LAST_ITEM="${LAST_ITEM}" \
  "${ROOT}/scripts/register_repo.sh" "${COMMON_ARGS[@]}"

printf 'ssm\nverifier\n' >"${MOCK_DIR}/expected-failure-order.log"
diff -u "${MOCK_DIR}/expected-failure-order.log" "$MOCK_LOG"
if [[ -e "$LAST_ITEM" ]]; then
  echo "FAIL: DynamoDB item was written after verifier failure" >&2
  exit 1
fi
