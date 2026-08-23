#!/usr/bin/env bash
set -euo pipefail

# Mocked regression tests for the fail-loud install probes. No AWS access:
# a fake `aws` on PATH simulates 200/404/403 responses.
#
# Covers:
#   bucket_exists.sh  -> 0 (exists) / 1 (404) / 2 (403 or other error)
#   table_exists.sh   -> 0 / 1 (ResourceNotFoundException) / 2 (other)
#   empty_bucket.sh   -> preserves the probe status (403 must NOT read as ok)
#   verify.sh codebuild predicate -> missing project must not pass
#
# Run: tests/install/test_probes.sh

SCRIPTS="$(cd "$(dirname "$0")/../../scripts" && pwd)"
MOCK_DIR="$(mktemp -d)"
trap 'rm -rf "$MOCK_DIR"' EXIT

write_mock() { # <behavior: ok|404|403|cb-missing|cb-present>
  cat >"$MOCK_DIR/aws" <<EOF
#!/usr/bin/env bash
case "$1" in
ok) exit 0 ;;
404) echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2; exit 254 ;;
403) echo "An error occurred (403) when calling the HeadBucket operation: Forbidden" >&2; exit 254 ;;
tnf) echo "An error occurred (ResourceNotFoundException) when calling the DescribeTable operation" >&2; exit 254 ;;
expired) echo "An error occurred (ExpiredToken) when calling the DescribeTable operation" >&2; exit 254 ;;
cb-missing) echo "None"; exit 0 ;;
cb-present) echo "openci-tf"; exit 0 ;;
esac
EOF
  chmod +x "$MOCK_DIR/aws"
}

FAILURES=0
expect_rc() { # <desc> <want_rc> <cmd...>
  local desc="$1" want="$2"
  shift 2
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  if [ "$got" = "$want" ]; then
    echo "PASS ${desc} (rc=${got})"
  else
    echo "FAIL ${desc}: want rc=${want}, got rc=${got}"
    FAILURES=$((FAILURES + 1))
  fi
}

export PATH="$MOCK_DIR:$PATH"

write_mock ok
expect_rc "bucket_exists: 200 -> 0" 0 "$SCRIPTS/bucket_exists.sh" some-bucket
write_mock 404
expect_rc "bucket_exists: 404 -> 1" 1 "$SCRIPTS/bucket_exists.sh" some-bucket
expect_rc "empty_bucket: 404 probe -> 0 (nothing to empty)" 0 "$SCRIPTS/empty_bucket.sh" some-bucket
write_mock 403
expect_rc "bucket_exists: 403 -> 2 (abort, not missing)" 2 "$SCRIPTS/bucket_exists.sh" some-bucket
expect_rc "empty_bucket: 403 probe -> 2 (must not report success)" 2 "$SCRIPTS/empty_bucket.sh" some-bucket

write_mock ok
expect_rc "table_exists: 200 -> 0" 0 "$SCRIPTS/table_exists.sh" some-table
write_mock tnf
expect_rc "table_exists: ResourceNotFound -> 1" 1 "$SCRIPTS/table_exists.sh" some-table
write_mock expired
expect_rc "table_exists: ExpiredToken -> 2 (abort)" 2 "$SCRIPTS/table_exists.sh" some-table

# codebuild predicate: CLI exits 0 for missing projects; the predicate must
# assert on the returned name.
codebuild_exists() { [ "$(aws codebuild batch-get-projects --names "$1" --query 'projects[0].name' --output text)" = "$1" ]; }
write_mock cb-missing
expect_rc "codebuild predicate: missing project -> nonzero" 1 codebuild_exists openci-tf
write_mock cb-present
expect_rc "codebuild predicate: present project -> 0" 0 codebuild_exists openci-tf

if [ "$FAILURES" -gt 0 ]; then
  echo "probe tests: ${FAILURES} failure(s)"
  exit 1
fi
echo "probe tests: all passed"
