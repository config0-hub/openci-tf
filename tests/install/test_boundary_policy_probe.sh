#!/usr/bin/env bash
# Tri-state boundary_policy_probe semantics for poweruser lifecycle checks.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../" && pwd)"
cd "$REPO"

failures=0
probe_script="$REPO/scripts/boundary_policy_probe.sh"
account_id="123456789012"
policy_name="openci-tf-executor-poweruser-permissions-boundary"

test_probe() {
  local name="$1" fake_rc="$2" fake_stderr="$3" expected_rc="$4"
  fake_aws="$(mktemp -d)"
  cat >"$fake_aws/aws" <<EOF
#!/usr/bin/env bash
if [ "\$1" = iam ] && [ "\$2" = get-policy ]; then
  printf '%s\\n' "$fake_stderr" >&2
  exit $fake_rc
fi
exit 99
EOF
  chmod +x "$fake_aws/aws"
  set +e
  PATH="$fake_aws:$PATH" "$probe_script" "$policy_name" "$account_id"
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

test_probe "present policy" 0 "" 0
test_probe "exact absent" 254 \
  "An error occurred (NoSuchEntity) when calling the GetPolicy operation: Policy not found" 1
test_probe "AWS CLI v2 exact absent" 254 \
  "aws: [ERROR]: An error occurred (NoSuchEntity) when calling the GetPolicy operation: Policy not found" 1
test_probe "generic 404 indeterminate" 1 \
  "404 Not Found from intermediary endpoint" 2
test_probe "access denied indeterminate" 254 \
  "An error occurred (AccessDenied) when calling the GetPolicy operation" 2
test_probe "wrong operation nosuchentity indeterminate" 254 \
  "An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found" 2
test_probe "spoofed AWS prefix indeterminate" 254 \
  "proxy aws: [ERROR]: An error occurred (NoSuchEntity) when calling the GetPolicy operation: Policy not found" 2
test_probe "truncated nosuchentity indeterminate" 254 \
  "An error occurred (NoSuchEntity) when calling the GetPolicy operation" 2
test_probe "multiline nosuchentity indeterminate" 254 \
  $'An error occurred (NoSuchEntity) when calling the GetPolicy operation: Policy not found\nextra diagnostic line' 2

if [ "$failures" -gt 0 ]; then
  echo "boundary_policy_probe tests: ${failures} failure(s)"
  exit 1
fi
echo "boundary_policy_probe tests: all passed"
