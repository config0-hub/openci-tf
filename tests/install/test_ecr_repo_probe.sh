#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROBE="$ROOT/scripts/ecr_repo_probe.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

run_case() {
  local name="$1" expected="$2" rc="$3" output="$4"
  cat >"$TMP/aws" <<EOF
#!/usr/bin/env bash
printf '%s\n' '$output'
exit $rc
EOF
  chmod +x "$TMP/aws"
  set +e
  PATH="$TMP:$PATH" "$PROBE" openci-tf us-east-1 >/dev/null 2>"$TMP/err"
  actual=$?
  set -e
  [ "$actual" -eq "$expected" ] || {
    echo "FAIL $name: expected $expected, got $actual" >&2
    cat "$TMP/err" >&2
    exit 1
  }
  echo "OK   $name"
}

run_case present 0 0 'openci-tf'
run_case exact_absent 1 254 'An error occurred (RepositoryNotFoundException) when calling the DescribeRepositories operation: repository not found'
run_case access_denied 2 254 'An error occurred (AccessDeniedException) when calling the DescribeRepositories operation: denied'
run_case generic_404 2 1 '404 Not Found from intermediary endpoint'
run_case wrapped_absence 2 254 'proxy: An error occurred (RepositoryNotFoundException) when calling the DescribeRepositories operation: repository not found'
run_case wrong_name 2 0 'other-repository'
run_case multiline 2 254 $'An error occurred (RepositoryNotFoundException) when calling the DescribeRepositories operation: missing\nextra diagnostic'

echo 'ecr_repo_probe tests: all passed'
