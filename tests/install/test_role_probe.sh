#!/usr/bin/env bash
# Tri-state role_probe semantics for uninstall optional poweruser removal.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../" && pwd)"
cd "$REPO"

failures=0
probe_script="$REPO/scripts/role_probe.sh"

test_probe() {
  local name="$1" role="$2" fake_rc="$3" fake_stderr="$4" expected_rc="$5"
  fake_aws="$(mktemp -d)"
  cat >"$fake_aws/aws" <<EOF
#!/usr/bin/env bash
if [ "\$1" = iam ] && [ "\$2" = get-role ]; then
  printf '%s\n' "$fake_stderr" >&2
  exit $fake_rc
fi
exit 99
EOF
  chmod +x "$fake_aws/aws"
  set +e
  PATH="$fake_aws:$PATH" "$probe_script" "$role"
  got=$?
  set -e
  rm -rf "$fake_aws"
  if [ "$got" -ne "$expected_rc" ]; then
    echo "FAIL $name expected rc=$expected_rc got rc=$got"
    failures=$((failures + 1))
  else
    echo "OK   $name"
  fi
}

test_probe "present role" "openci-tf-executor-poweruser" 0 "" 0
test_probe "exact absent" "openci-tf-executor-poweruser" 254 \
  "An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found" 1
test_probe "AWS CLI v2 exact absent" "openci-tf-executor-poweruser" 254 \
  "aws: [ERROR]: An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found" 1
test_probe "generic 404 indeterminate" "openci-tf-executor-poweruser" 1 \
  "404 Not Found from intermediary endpoint" 2
test_probe "bare not found indeterminate" "openci-tf-executor-poweruser" 1 "Not Found" 2
test_probe "access denied indeterminate" "openci-tf-executor-poweruser" 254 \
  "An error occurred (AccessDenied) when calling the GetRole operation" 2
test_probe "throttle indeterminate" "openci-tf-executor-poweruser" 254 \
  "An error occurred (Throttling) when calling the GetRole operation" 2
test_probe "malformed json indeterminate" "openci-tf-executor-poweruser" 1 \
  "Expecting value: line 1 column 1 (char 0)" 2
test_probe "wrong operation nosuchentity indeterminate" "openci-tf-executor-poweruser" 254 \
  "An error occurred (NoSuchEntity) when calling the GetPolicy operation: Policy not found" 2
test_probe "wrapper token nosuchentity indeterminate" "openci-tf-executor-poweruser" 1 \
  "proxy wrapper: (NoSuchEntity) from intermediary" 2
test_probe "wrapper full phrase indeterminate" "openci-tf-executor-poweruser" 1 \
  "proxy wrapper: An error occurred (NoSuchEntity) when calling the GetRole operation but the upstream response was not an AWS CLI error" 2
test_probe "spoofed AWS prefix indeterminate" "openci-tf-executor-poweruser" 254 \
  "proxy aws: [ERROR]: An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found" 2
test_probe "truncated nosuchentity indeterminate" "openci-tf-executor-poweruser" 254 \
  "An error occurred (NoSuchEntity) when calling the GetRole operation" 2
test_probe "multiline nosuchentity indeterminate" "openci-tf-executor-poweruser" 254 \
  $'An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found\nextra diagnostic line' 2
test_probe "nosuchentity wrong rc indeterminate" "openci-tf-executor-poweruser" 255 \
  "An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found" 2

if [ "$failures" -gt 0 ]; then
  echo "role_probe tests: ${failures} failure(s)"
  exit 1
fi
echo "role_probe tests: all passed"
