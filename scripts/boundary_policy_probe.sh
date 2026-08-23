#!/usr/bin/env bash
# Tri-state IAM customer-managed policy existence probe.
# Exit 0 = present, 1 = exact not-found (NoSuchEntity), 2 = indeterminate.
set -euo pipefail

POLICY_NAME="${1:?Usage: boundary_policy_probe.sh <policy_name> <account_id>}"
ACCOUNT_ID="${2:?Usage: boundary_policy_probe.sh <policy_name> <account_id>}"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"

err="$(mktemp)"
set +e
aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>"$err"
probe_rc=$?
set -e

if [ "$probe_rc" -eq 0 ]; then
  rm -f "$err"
  exit 0
fi

# Only AWS CLI IAM service errors (exit 254) with a single stderr line matching
# the complete GetPolicy NoSuchEntity signature are treated as absent. Wrappers,
# wrong operations, generic tokens, other exit codes, and multiline stderr are
# indeterminate — callers must not treat them as absence.
NOSUCH_LINE_RE='^(aws: \[ERROR\]: )?An error occurred \(NoSuchEntity\) when calling the GetPolicy operation: .+$'

if [ "$probe_rc" -ne 254 ]; then
  cat "$err" >&2
  rm -f "$err"
  exit 2
fi

non_empty=0
matched=0
while IFS= read -r line || [ -n "$line" ]; do
  if [[ "$line" =~ ^[[:space:]]*$ ]]; then
    continue
  fi
  non_empty=$((non_empty + 1))
  if [[ "$line" =~ $NOSUCH_LINE_RE ]]; then
    matched=$((matched + 1))
  fi
done <"$err"

if [ "$non_empty" -eq 1 ] && [ "$matched" -eq 1 ]; then
  rm -f "$err"
  exit 1
fi

cat "$err" >&2
rm -f "$err"
exit 2
