#!/usr/bin/env bash
set -euo pipefail

# Mocked ownership-abort tests for `just bootstrap` / `just bootstrap-destroy`.
# No AWS, no terraform. Proves the destructive paths REFUSE to touch resources
# the installer does not own, and that no delete call is ever issued on abort.
#
# Scenarios:
#   A) bootstrap: existing bucket with a foreign ManagedBy tag -> abort (rc 1)
#   B) bootstrap-destroy: foreign-owned bucket -> abort, NO delete-objects call
#   C) bootstrap-destroy: unreadable tags (AccessDenied) -> abort rc 2
#   E) bootstrap: surviving LOCAL state + foreign-owned bucket -> abort (no adopt)
#   F) bootstrap: surviving LOCAL state tracking a DIFFERENT bucket -> abort
#   G) bootstrap-destroy: local state + foreign bucket -> abort, NO deletes
# No DynamoDB lock table exists (S3 native lock file); no recipe may ever call
# dynamodb delete-table.
#
# Run: tests/install/test_bootstrap_ownership.sh

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
MOCK_DIR="$(mktemp -d)"
export MOCK_CALLS="$MOCK_DIR/calls"
trap 'rm -rf "$MOCK_DIR"' EXIT

write_mock() { # <tagging: foreign|denied> <bucket: present|absent> <table: present|absent>
  cat >"$MOCK_DIR/aws" <<EOF
#!/usr/bin/env bash
mkdir -p "\$MOCK_CALLS"
case "\$*" in
*"sts get-caller-identity"*) echo "123456789012"; exit 0 ;;
*"s3api head-bucket"*)
  if [ "$2" = absent ]; then echo "An error occurred (404): Not Found" >&2; exit 254; fi
  exit 0 ;;
*"s3api get-bucket-tagging"*)
  if [ "$1" = denied ]; then echo "An error occurred (AccessDenied) when calling the GetBucketTagging operation" >&2; exit 254; fi
  if [ "$1" = owned-bucket-foreign-table ]; then echo "openci-tf-bootstrap"; else echo "someone-else"; fi
  exit 0 ;;
*"dynamodb describe-table"*)
  if [ "$3" = absent ]; then echo "An error occurred (ResourceNotFoundException)" >&2; exit 254; fi
  exit 0 ;;
*"dynamodb list-tags-of-resource"*) echo "someone-else"; exit 0 ;;
*"dynamodb delete-table"*) touch "\$MOCK_CALLS/delete-table"; exit 0 ;;
*"s3api delete-objects"*|*"s3api list-object-versions"*) touch "\$MOCK_CALLS/delete-objects"; echo '{"Objects": []}'; exit 0 ;;
*) exit 0 ;;
esac
EOF
  cat >"$MOCK_DIR/terraform" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$MOCK_DIR/aws" "$MOCK_DIR/terraform"
}

export PATH="$MOCK_DIR:$PATH"
FAILURES=0
run_case() { # <desc> <want_rc> <recipe>
  local desc="$1" want="$2" recipe="$3" got=0
  rm -rf "$MOCK_CALLS"
  [ "${KEEP_LOCAL_STATE:-0}" = 1 ] || rm -f "$REPO/infra/bootstrap/terraform.tfstate"
  just --justfile "$REPO/justfile" --working-directory "$REPO" "$recipe" >/dev/null 2>&1 || got=$?
  if [ "$got" = "$want" ]; then
    echo "PASS ${desc} (rc=${got})"
  else
    echo "FAIL ${desc}: want rc=${want}, got rc=${got}"
    FAILURES=$((FAILURES + 1))
  fi
}

write_local_state() { # <bucket-name-in-state> [table-name-in-state]
  local table_json=""
  if [ -n "${2:-}" ]; then
    table_json=", {\"mode\": \"managed\", \"type\": \"aws_dynamodb_table\", \"name\": \"locks\", \"instances\": [{\"attributes\": {\"name\": \"$2\"}}]}"
  fi
  cat >"$REPO/infra/bootstrap/terraform.tfstate" <<EOF
{"version": 4, "resources": [{"mode": "managed", "type": "aws_s3_bucket", "name": "state",
  "instances": [{"attributes": {"bucket": "$1"}}]}${table_json}]}
EOF
}

write_raw_state() { # <resources-json-array>
  cat >"$REPO/infra/bootstrap/terraform.tfstate" <<EOF
{"version": 4, "resources": $1}
EOF
}
assert_no_call() { # <desc> <marker>
  if [ -e "$MOCK_CALLS/$2" ]; then
    echo "FAIL ${1}: forbidden call '$2' was issued"
    FAILURES=$((FAILURES + 1))
  else
    echo "PASS ${1}"
  fi
}

write_mock foreign present present
run_case "A: bootstrap aborts on foreign-owned bucket" 1 bootstrap
run_case "B: bootstrap-destroy aborts on foreign-owned bucket" 1 bootstrap-destroy
assert_no_call "B: no delete-objects issued on abort" delete-objects
assert_no_call "B: no delete-table issued on abort" delete-table

write_mock denied present present
run_case "C: bootstrap-destroy aborts on unreadable tags" 2 bootstrap-destroy

# E/F/G: surviving local state scenarios
write_mock foreign present present
write_local_state "openci-tf-state-123456789012"
KEEP_LOCAL_STATE=1 run_case "E: local-state resume aborts on foreign-owned bucket" 1 bootstrap
write_local_state "some-other-bucket"
KEEP_LOCAL_STATE=1 run_case "F: local state tracking wrong bucket aborts" 1 bootstrap
write_local_state "openci-tf-state-123456789012"
KEEP_LOCAL_STATE=1 run_case "G: local-state destroy aborts on foreign-owned bucket" 1 bootstrap-destroy
assert_no_call "G: no delete-objects issued" delete-objects
assert_no_call "G: no delete-table issued" delete-table
rm -f "$REPO/infra/bootstrap/terraform.tfstate"

# K/L/M: state-identity address/mode strictness (reviewer counterexamples).
# Mock 'foreign' tagging would abort anyway on live checks, so use the
# owned-bucket mock: only the identity predicate can cause these aborts.
write_mock owned-bucket-foreign-table present absent
write_raw_state '[{"mode": "managed", "type": "aws_s3_bucket", "name": "not_state", "instances": [{"attributes": {"bucket": "openci-tf-state-123456789012"}}]}]'
KEEP_LOCAL_STATE=1 run_case "K: wrong managed address (not_state) aborts" 1 bootstrap-destroy
assert_no_call "K: no delete-objects issued" delete-objects
write_raw_state '[{"mode": "data", "type": "aws_s3_bucket", "name": "state", "instances": [{"attributes": {"bucket": "openci-tf-state-123456789012"}}]}]'
KEEP_LOCAL_STATE=1 run_case "L: data-source-only state aborts" 1 bootstrap-destroy
assert_no_call "L: no delete-objects issued" delete-objects
write_raw_state '[{"mode": "managed", "type": "aws_s3_bucket", "name": "state", "instances": [{"attributes": {"bucket": "openci-tf-state-123456789012"}}]}, {"mode": "managed", "type": "aws_iam_role", "name": "foreign", "instances": [{"attributes": {"name": "someone-elses-role"}}]}]'
KEEP_LOCAL_STATE=1 run_case "M: unrelated managed resource in state aborts" 1 bootstrap-destroy
assert_no_call "M: no delete-objects issued" delete-objects
KEEP_LOCAL_STATE=1 run_case "M2: unrelated managed resource aborts bootstrap resume" 1 bootstrap

# N: allowlisted S3 child address physically bound to a FOREIGN bucket
write_raw_state '[{"mode": "managed", "type": "aws_s3_bucket", "name": "state", "instances": [{"attributes": {"bucket": "openci-tf-state-123456789012"}}]}, {"mode": "managed", "type": "aws_s3_bucket_public_access_block", "name": "state", "instances": [{"attributes": {"bucket": "someone-elses-bucket"}}]}]'
KEEP_LOCAL_STATE=1 run_case "N: foreign-bucket public-access-block in state aborts destroy" 1 bootstrap-destroy
assert_no_call "N: no delete-objects issued" delete-objects
KEEP_LOCAL_STATE=1 run_case "N2: foreign-bucket child aborts bootstrap resume" 1 bootstrap

# O: duplicate entries for one allowlisted address — an S3 child duplicated
# with BOTH instances bound to the expected bucket, so only the generic
# duplicate-address predicate (not the table-count or bucket-binding guards)
# can reject it.
write_raw_state '[{"mode": "managed", "type": "aws_s3_bucket", "name": "state", "instances": [{"attributes": {"bucket": "openci-tf-state-123456789012"}}]}, {"mode": "managed", "type": "aws_s3_bucket_public_access_block", "name": "state", "instances": [{"attributes": {"bucket": "openci-tf-state-123456789012"}}]}, {"mode": "managed", "type": "aws_s3_bucket_public_access_block", "name": "state", "instances": [{"attributes": {"bucket": "openci-tf-state-123456789012"}}]}]'
KEEP_LOCAL_STATE=1 run_case "O: duplicate allowlisted address aborts destroy" 1 bootstrap-destroy
assert_no_call "O: no delete-objects issued" delete-objects
rm -f "$REPO/infra/bootstrap/terraform.tfstate"

if [ "$FAILURES" -gt 0 ]; then
  echo "bootstrap ownership tests: ${FAILURES} failure(s)"
  exit 1
fi
echo "bootstrap ownership tests: all passed"
