#!/usr/bin/env bash
set -euo pipefail

# Hermetic tests for target onboarding helpers and recipes.
# No real AWS, terraform, or deploy. Mocks aws/just on PATH.
#
# Run: tests/install/test_target_onboarding.sh

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPTS="${REPO}/scripts"
MOCK_DIR="$(mktemp -d)"
export MOCK_CALLS="$MOCK_DIR/calls"
export SSM_STORE="$MOCK_DIR/ssm"
mkdir -p "$SSM_STORE" "$MOCK_CALLS"
trap 'rm -rf "$MOCK_DIR"' EXIT

FAILURES=0

write_aws_mock() { # <target-account> <bucket-exists: yes|no|denied>
  local target_acct="$1" bucket="$2"
  cat >"$MOCK_DIR/aws" <<EOF
#!/usr/bin/env bash
mkdir -p "\$MOCK_CALLS"
case "\$*" in
*"sts get-caller-identity"*)
  echo "$target_acct"
  exit 0
  ;;
*"s3api head-bucket"*)
  touch "\$MOCK_CALLS/head-bucket"
  case "$bucket" in
    yes) exit 0 ;;
    no) echo "An error occurred (404): Not Found" >&2; exit 254 ;;
    denied) echo "An error occurred (403): Forbidden" >&2; exit 254 ;;
  esac
  ;;
*"ssm put-parameter"*)
  touch "\$MOCK_CALLS/ssm-put"
  name="" value=""
  while [ \$# -gt 0 ]; do
    case "\$1" in
      --name) name="\$2"; shift 2 ;;
      --value) value="\$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  key="\${name##*/}"
  if [ "\$key" = "target_account_ids" ]; then
    echo append-target-id >>"\$MOCK_CALLS/sequence"
  fi
  printf '%s' "\$value" >"\$SSM_STORE/\$key"
  exit 0
  ;;
*"ssm get-parameter"*)
  name=""
  while [ \$# -gt 0 ]; do
    case "\$1" in
      --name) name="\$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  key="\${name##*/}"
  if [ -f "\$SSM_STORE/\$key" ]; then
    cat "\$SSM_STORE/\$key"
    exit 0
  fi
  echo "ParameterNotFound" >&2
  exit 254
  ;;
*"ssm get-parameters-by-path"*)
  exit 0
  ;;
*"dynamodb describe-table"*)
  touch "\$MOCK_CALLS/dynamodb-describe-table"
  echo "ACTIVE"
  exit 0
  ;;
*"dynamodb put-item"*)
  touch "\$MOCK_CALLS/dynamodb-put"
  printf '%s\n' "\$*" >"\$MOCK_CALLS/dynamodb-put-args"
  item=""
  while [ \$# -gt 0 ]; do
    case "\$1" in
      --item) item="\$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  printf '%s' "\$item" >"\$SSM_STORE/account-row"
  echo register-alias >>"\$MOCK_CALLS/sequence"
  exit 0
  ;;
*)
  echo "unexpected aws call: \$*" >&2
  exit 99
  ;;
esac
EOF
  chmod +x "$MOCK_DIR/aws"
}

write_just_mock() {
  cat >"$MOCK_DIR/just" <<'EOF'
#!/usr/bin/env bash
mkdir -p "$MOCK_CALLS"
echo "$*" >>"$MOCK_CALLS/just"
if [ "$1" = "deploy" ]; then
  echo deploy >>"$MOCK_CALLS/sequence"
fi
exit 0
EOF
  chmod +x "$MOCK_DIR/just"
}

write_failing_deploy_just_mock() {
  cat >"$MOCK_DIR/just" <<'EOF'
#!/usr/bin/env bash
mkdir -p "$MOCK_CALLS"
echo "$*" >>"$MOCK_CALLS/just"
if [ "$1" = "deploy" ]; then
  echo deploy >>"$MOCK_CALLS/sequence"
  exit 1
fi
exit 0
EOF
  chmod +x "$MOCK_DIR/just"
}

expect_rc() { # <desc> <want> <cmd...>
  local desc="$1" want="$2"
  shift 2
  local got=0
  (cd "$REPO" && "$@") >/dev/null 2>&1 || got=$?
  if [ "$got" = "$want" ]; then
    echo "PASS ${desc} (rc=${got})"
  else
    echo "FAIL ${desc}: want rc=${want}, got rc=${got}"
    FAILURES=$((FAILURES + 1))
  fi
}

assert_file_contains() { # <desc> <file>
  local desc="$1" file="$2"
  if [ -s "$file" ]; then
    echo "PASS ${desc}"
  else
    echo "FAIL ${desc}: empty or missing $file"
    FAILURES=$((FAILURES + 1))
  fi
}

assert_no_call() { # <desc> <marker>
  if [ -e "$MOCK_CALLS/$2" ]; then
    echo "FAIL ${1}: forbidden call '$2' was issued"
    FAILURES=$((FAILURES + 1))
  else
    echo "PASS ${1}"
  fi
}

assert_account_row_json() { # <desc> <expected-alias> <expected-max-ttl>
  local desc="$1" expected_alias="$2" expected_max_ttl="$3"
  if python3 - "$SSM_STORE/account-row" "$expected_alias" "$expected_max_ttl" <<'PY'
import json
import sys

path, expected_alias, expected_max_ttl = sys.argv[1:]
raw = open(path, encoding="utf-8").read()
top_level_pairs = []

def hook(items):
    if items and all(isinstance(value, dict) for _, value in items):
        top_level_pairs.append(items)
    return dict(items)

data = json.loads(raw, object_pairs_hook=hook)
top = top_level_pairs[-1]
assert sum(1 for key, _ in top if key == "sk") == 1, top
assert data["sk"] == {"S": expected_alias}, data["sk"]
assert data["pk"] == {"S": "account"}
assert data["account_id"] == {"S": "REPLACE_SECONDARY_ACCOUNT"}
assert data["role_name"] == {"S": "openci-tf-executor-readonly"}
assert data["external_id"] == {"S": "openci-tf-a6e196030e40a73e"}
if expected_max_ttl:
    assert data["max_ttl"] == {"N": expected_max_ttl}, data["max_ttl"]
else:
    assert "max_ttl" not in data
PY
  then
    echo "PASS ${desc}"
  else
    echo "FAIL ${desc}"
    FAILURES=$((FAILURES + 1))
  fi
}

export PATH="$MOCK_DIR:$PATH"
export OPENCI_TF_PROJECT=openci-tf

# derive_external_id: validation + determinism
expect_rc "derive_external_id rejects short hub id" 1 "$SCRIPTS/derive_external_id.sh" 123 REPLACE_SECONDARY_ACCOUNT
expect_rc "derive_external_id rejects target letters" 1 "$SCRIPTS/derive_external_id.sh" REPLACE_MAIN_ACCOUNT abcdefghijkl
ID1="$($SCRIPTS/derive_external_id.sh REPLACE_MAIN_ACCOUNT REPLACE_SECONDARY_ACCOUNT)"
ID2="$($SCRIPTS/derive_external_id.sh REPLACE_MAIN_ACCOUNT REPLACE_SECONDARY_ACCOUNT)"
ID3="$($SCRIPTS/derive_external_id.sh 111111111111 REPLACE_SECONDARY_ACCOUNT)"
[[ "$ID1" = "$ID2" ]] && echo "PASS derive_external_id is deterministic" || { echo "FAIL derive_external_id mismatch"; FAILURES=$((FAILURES + 1)); }
[[ "$ID1" != "$ID3" ]] && echo "PASS derive_external_id varies by hub account" || { echo "FAIL derive_external_id ignores hub account"; FAILURES=$((FAILURES + 1)); }
[[ "$ID1" = "openci-tf-a6e196030e40a73e" ]] && echo "PASS derive_external_id known vector" || { echo "FAIL derive_external_id vector got $ID1"; FAILURES=$((FAILURES + 1)); }
[[ "$ID1" =~ ^openci-tf-[0-9a-f]{16}$ ]] && echo "PASS derive_external_id format" || { echo "FAIL derive_external_id format"; FAILURES=$((FAILURES + 1)); }

# bucket_from_s3_arn
expect_rc "bucket_from_s3_arn rejects garbage" 1 "$SCRIPTS/bucket_from_s3_arn.sh" not-an-arn
BUCKET_NAME="$("$SCRIPTS/bucket_from_s3_arn.sh" arn:aws:s3:::openci-tf-state-REPLACE_SECONDARY_ACCOUNT)"
[[ "$BUCKET_NAME" = "openci-tf-state-REPLACE_SECONDARY_ACCOUNT" ]] && echo "PASS bucket_from_s3_arn extracts name" || { echo "FAIL bucket_from_s3_arn"; FAILURES=$((FAILURES + 1)); }

# append_target_account_id: unique append + jq validation
write_aws_mock REPLACE_MAIN_ACCOUNT yes
write_just_mock
printf '%s' '["REPLACE_MAIN_ACCOUNT"]' >"$SSM_STORE/target_account_ids"
(cd "$REPO" && "$SCRIPTS/append_target_account_id.sh" REPLACE_SECONDARY_ACCOUNT) >/dev/null
UPDATED="$(jq -c . "$SSM_STORE/target_account_ids")"
[[ "$UPDATED" = '["REPLACE_MAIN_ACCOUNT","REPLACE_SECONDARY_ACCOUNT"]' ]] && echo "PASS append_target_account_id appends uniquely" || { echo "FAIL append got $UPDATED"; FAILURES=$((FAILURES + 1)); }
(cd "$REPO" && "$SCRIPTS/append_target_account_id.sh" REPLACE_SECONDARY_ACCOUNT) >/dev/null
[[ "$(jq -c . "$SSM_STORE/target_account_ids")" = "$UPDATED" ]] && echo "PASS append_target_account_id is idempotent" || { echo "FAIL append not idempotent"; FAILURES=$((FAILURES + 1)); }
printf '%s' 'not-json' >"$SSM_STORE/target_account_ids"
expect_rc "append_target_account_id rejects malformed JSON" 1 bash -c "cd '$REPO' && '$SCRIPTS/append_target_account_id.sh' REPLACE_SECONDARY_ACCOUNT"

# target_connect_state_bucket uses SSM ARN when configured
printf '%s' 'arn:aws:s3:::custom-target-bucket' >"$SSM_STORE/target_state_bucket_arn"
RESOLVED="$(cd "$REPO" && "$SCRIPTS/target_connect_state_bucket.sh")"
[[ "$RESOLVED" = "custom-target-bucket" ]] && echo "PASS target_connect_state_bucket reads SSM ARN" || { echo "FAIL resolved=$RESOLVED"; FAILURES=$((FAILURES + 1)); }
rm -f "$SSM_STORE/target_state_bucket_arn"
DEFAULT="$(cd "$REPO" && "$SCRIPTS/target_connect_state_bucket.sh")"
[[ "$DEFAULT" = "openci-tf-state-REPLACE_MAIN_ACCOUNT" ]] && echo "PASS target_connect_state_bucket defaults to conventional name" || { echo "FAIL default=$DEFAULT"; FAILURES=$((FAILURES + 1)); }

# register_account: safely encodes arbitrary aliases as DynamoDB attribute JSON
rm -rf "${MOCK_CALLS:?}"/* "${SSM_STORE:?}"/*
write_aws_mock REPLACE_MAIN_ACCOUNT yes
ALIAS_PAYLOAD=$'qa"alias\\with{braces}\n,"account_id":{"S":"000000000000"},"sk":{"S":"pwn"}'
(cd "$REPO" && "$SCRIPTS/register_account.sh" --alias "$ALIAS_PAYLOAD" --account-id REPLACE_SECONDARY_ACCOUNT --role-name openci-tf-executor-readonly --max-ttl 0900) >/dev/null
assert_account_row_json "register_account emits valid JSON without alias injection or data loss" "$ALIAS_PAYLOAD" "900"

# target_onboard: bucket verification + SSM writes + target-connect invocation
rm -rf "$MOCK_CALLS"/* "$SSM_STORE"/*
write_aws_mock REPLACE_SECONDARY_ACCOUNT yes
write_just_mock
(cd "$REPO" && "$SCRIPTS/target_onboard.sh" --hub-account-id REPLACE_MAIN_ACCOUNT) >/dev/null
if [ ! -e "$SSM_STORE/external_id" ]; then
  echo "PASS target_onboard does not store external_id"
else
  echo "FAIL target_onboard stored external_id"
  FAILURES=$((FAILURES + 1))
fi
assert_file_contains "target_onboard stores hub role arn" "$SSM_STORE/hub_lambda_exec_role_arn"
assert_file_contains "target_onboard stores target bucket arn" "$SSM_STORE/target_state_bucket_arn"
[[ "$(cat "$SSM_STORE/hub_lambda_exec_role_arn")" = "arn:aws:iam::REPLACE_MAIN_ACCOUNT:role/openci-tf-hub-lambda-exec" ]] && echo "PASS target_onboard hub role arn value" || { echo "FAIL hub role arn"; FAILURES=$((FAILURES + 1)); }
[[ "$(cat "$SSM_STORE/target_state_bucket_arn")" = "arn:aws:s3:::openci-tf-state-REPLACE_SECONDARY_ACCOUNT" ]] && echo "PASS target_onboard bucket arn value" || { echo "FAIL bucket arn"; FAILURES=$((FAILURES + 1)); }
if [ -f "$MOCK_CALLS/just" ] && grep -Fq "target-create-aws-readonly" "$MOCK_CALLS/just"; then
  echo "PASS target_onboard invokes target-create-aws-readonly"
else
  echo "FAIL target_onboard missing target-create-aws-readonly invocation"
  FAILURES=$((FAILURES + 1))
fi
assert_no_call "target_onboard does not create buckets" head-bucket-create

# target_onboard: missing bucket fails loud
rm -rf "$MOCK_CALLS"/* "$SSM_STORE"/*
write_aws_mock REPLACE_SECONDARY_ACCOUNT no
write_just_mock
expect_rc "target_onboard fails when bucket missing" 1 bash -c "cd '$REPO' && '$SCRIPTS/target_onboard.sh' --hub-account-id REPLACE_MAIN_ACCOUNT"
assert_no_call "target_onboard missing bucket skips target-create-aws-readonly" just

# target_onboard: denied bucket probe fails loud (not treated as missing)
rm -rf "$MOCK_CALLS"/* "$SSM_STORE"/*
write_aws_mock REPLACE_SECONDARY_ACCOUNT denied
write_just_mock
expect_rc "target_onboard fails on denied bucket probe" 2 bash -c "cd '$REPO' && '$SCRIPTS/target_onboard.sh' --hub-account-id REPLACE_MAIN_ACCOUNT"

# register_target: append + deploy + register alias (alias row last)
rm -rf "$MOCK_CALLS"/* "$SSM_STORE"/*
write_aws_mock REPLACE_MAIN_ACCOUNT yes
write_just_mock
printf '%s' '["REPLACE_MAIN_ACCOUNT"]' >"$SSM_STORE/target_account_ids"
printf '%s' 'legacy-external-id' >"$SSM_STORE/account-row"
(cd "$REPO" && "$SCRIPTS/register_target.sh" --alias platform-test2 --account-id REPLACE_SECONDARY_ACCOUNT) >/dev/null
assert_file_contains "register_target invokes deploy marker" "$MOCK_CALLS/just"
if [ -f "$MOCK_CALLS/just" ] && grep -Fq "deploy" "$MOCK_CALLS/just"; then
  echo "PASS register_target invokes deploy"
else
  echo "FAIL register_target missing deploy invocation"
  FAILURES=$((FAILURES + 1))
fi
[[ -e "$MOCK_CALLS/dynamodb-put" ]] && echo "PASS register_target registers account row" || { echo "FAIL register_target missing dynamodb put"; FAILURES=$((FAILURES + 1)); }
if grep -Fq 'openci-tf-a6e196030e40a73e' "$MOCK_CALLS/dynamodb-put-args"; then
  echo "PASS register_target stores derived external_id"
else
  echo "FAIL register_target missing derived external_id"
  FAILURES=$((FAILURES + 1))
fi
if grep -Fq 'openci-tf-a6e196030e40a73e' "$SSM_STORE/account-row" && ! grep -Fq 'legacy-external-id' "$SSM_STORE/account-row"; then
  echo "PASS register_target replaces legacy external_id"
else
  echo "FAIL register_target did not replace legacy external_id"
  FAILURES=$((FAILURES + 1))
fi
[[ "$(jq -c . "$SSM_STORE/target_account_ids")" = '["REPLACE_MAIN_ACCOUNT","REPLACE_SECONDARY_ACCOUNT"]' ]] && echo "PASS register_target appends target_account_ids" || { echo "FAIL register_target ids"; FAILURES=$((FAILURES + 1)); }
SEQUENCE="$(tr '\n' ' ' <"$MOCK_CALLS/sequence" | sed 's/ $//')"
[[ "$SEQUENCE" = "append-target-id deploy register-alias" ]] && echo "PASS register_target operation order" || { echo "FAIL register_target order got '$SEQUENCE'"; FAILURES=$((FAILURES + 1)); }

# register_target: failed deploy must not publish alias row
rm -rf "$MOCK_CALLS"/* "$SSM_STORE"/*
write_aws_mock REPLACE_MAIN_ACCOUNT yes
write_failing_deploy_just_mock
printf '%s' '["REPLACE_MAIN_ACCOUNT"]' >"$SSM_STORE/target_account_ids"
expect_rc "register_target fails loud when deploy fails" 1 bash -c "cd '$REPO' && '$SCRIPTS/register_target.sh' --alias platform-test2 --account-id REPLACE_SECONDARY_ACCOUNT"
assert_no_call "register_target deploy failure skips alias row" dynamodb-put
[[ "$(jq -c . "$SSM_STORE/target_account_ids")" = '["REPLACE_MAIN_ACCOUNT","REPLACE_SECONDARY_ACCOUNT"]' ]] && echo "PASS register_target deploy failure still appends target id" || { echo "FAIL register_target deploy failure ids"; FAILURES=$((FAILURES + 1)); }

expect_rc "register_target rejects invalid account id" 1 bash -c "cd '$REPO' && '$SCRIPTS/register_target.sh' --alias bad --account-id short"

if [ "$FAILURES" -gt 0 ]; then
  echo "target onboarding tests: ${FAILURES} failure(s)"
  exit 1
fi
echo "target onboarding tests: all passed"
