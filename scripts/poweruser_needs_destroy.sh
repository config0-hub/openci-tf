#!/usr/bin/env bash
# Decide whether target-connect-poweruser destroy must run during uninstall.
# Exit 0 = destroy required (role and/or boundary policy present)
# Exit 1 = skip (both exactly absent)
# Exit 2 = abort (indeterminate probe)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
ROLE="${PROJECT}-executor-poweruser"
BOUNDARY="${ROLE}-permissions-boundary"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

set +e
"$SCRIPT_DIR/role_probe.sh" "$ROLE"
role_rc=$?
"$SCRIPT_DIR/boundary_policy_probe.sh" "$BOUNDARY" "$ACCOUNT_ID"
boundary_rc=$?
set -e

if [ "$role_rc" -eq 2 ] || [ "$boundary_rc" -eq 2 ]; then
  echo "ERROR: indeterminate poweruser footprint probe (role rc=${role_rc}, boundary rc=${boundary_rc})" >&2
  exit 2
fi
if [ "$role_rc" -eq 0 ] || [ "$boundary_rc" -eq 0 ]; then
  exit 0
fi
exit 1
