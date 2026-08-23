#!/usr/bin/env bash
# readonly_needs_destroy decision semantics for uninstall.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../" && pwd)"
cd "$REPO"

failures=0
decision_script="$REPO/scripts/readonly_needs_destroy.sh"

test_decision() {
  local name="$1" role_rc="$2" role_stderr="$3" policy_rc="$4" policy_stderr="$5" expected_rc="$6"
  fake_aws="$(mktemp -d)"
  cat >"$fake_aws/aws" <<EOF
#!/usr/bin/env bash
if [ "\$1" = sts ] && [ "\$2" = get-caller-identity ]; then
  echo "123456789012"
  exit 0
fi
if [ "\$1" = iam ] && [ "\$2" = get-role ]; then
  printf '%s\\n' "$role_stderr" >&2
  exit $role_rc
fi
if [ "\$1" = iam ] && [ "\$2" = get-policy ]; then
  printf '%s\\n' "$policy_stderr" >&2
  exit $policy_rc
fi
exit 99
EOF
  chmod +x "$fake_aws/aws"
  set +e
  PATH="$fake_aws:$PATH" "$decision_script"
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

nosuch_role='An error occurred (NoSuchEntity) when calling the GetRole operation: Role not found'
nosuch_policy='An error occurred (NoSuchEntity) when calling the GetPolicy operation: Policy not found'

test_decision "both absent skip destroy" 254 "$nosuch_role" 254 "$nosuch_policy" 1
test_decision "role present destroy" 0 "" 254 "$nosuch_policy" 0
test_decision "policy only partial destroy" 254 "$nosuch_role" 0 "" 0
test_decision "both present destroy" 0 "" 0 "" 0
test_decision "role access denied abort" 254 \
  "An error occurred (AccessDenied) when calling the GetRole operation" 254 "$nosuch_policy" 2

if [ "$failures" -gt 0 ]; then
  echo "readonly_needs_destroy tests: ${failures} failure(s)"
  exit 1
fi
echo "readonly_needs_destroy tests: all passed"
