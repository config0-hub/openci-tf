#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MOCK_DIR="$(mktemp -d)"
trap 'rm -rf "$MOCK_DIR"' EXIT

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

write_ssm_config_mock() {
  mkdir -p "${MOCK_DIR}/scripts"
  cat >"${MOCK_DIR}/scripts/ssm_config.sh" <<'EOF'
#!/usr/bin/env bash
case "$2" in
  repo) echo "org/repo" ;;
  pr_number) echo "7" ;;
  folder) echo "infra/vpc" ;;
  trigger_id) echo "trigger-smoke" ;;
  *) echo "unknown-key" >&2; exit 1 ;;
esac
EOF
  chmod +x "${MOCK_DIR}/scripts/ssm_config.sh"
}

write_gh_comment_marker() {
  local api_body="${1:-}"
  cat >"${MOCK_DIR}/gh" <<EOF
#!/usr/bin/env bash
if [[ "\$1" == "pr" && "\$2" == "comment" ]]; then
  touch "${MOCK_DIR}/comment-posted"
  exit 0
fi
if [[ "\$1" == "api" ]]; then
${api_body}
  echo "https://github.example/org/repo/pull/7#issuecomment-1"
  exit 0
fi
echo "unexpected gh call: \$*" >&2
exit 97
EOF
  chmod +x "${MOCK_DIR}/gh"
}

write_common_mocks() {
  write_gh_comment_marker '  if [[ "$*" == *"updated_at"* ]]; then echo "2026-08-05T10:00:01Z"; exit 0; fi'
  cat >"${MOCK_DIR}/terraform" <<'EOF'
#!/usr/bin/env bash
echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
EOF
  chmod +x "${MOCK_DIR}/terraform"
}

run_smoke() {
  rm -f "${MOCK_DIR}/comment-posted"
  cd "${ROOT}"
  PATH="${MOCK_DIR}:${PATH}"
  SSM_CONFIG_PROJECT=test-smoke \
  SMOKE_SSM_CONFIG_SCRIPT="${MOCK_DIR}/scripts/ssm_config.sh" \
  SMOKE_STARTED_AT="2026-08-05T09:59:30Z" \
  SMOKE_WAIT_EXECUTIONS=1 \
  SMOKE_WAIT_TERMINAL=1 \
  SMOKE_POLL_SECONDS=0 \
  ./scripts/smoke.sh
}

GOOD_INPUT='{"webhook_info":{"trigger_id":"trigger-smoke","repo_name":"org/repo","pr_number":7,"comment_body":"tf plan infra/vpc"},"folders":["infra/vpc"]}'
GOOD_OUTPUT='{"rendered":true,"outcomes":[{"folder":"infra/vpc","output":{"exec_id":"run.folder.0","status":"succeeded"}}]}'

write_ssm_config_mock
write_common_mocks

expect_rc "smoke rejects unrelated execution" 1 env PATH="${MOCK_DIR}:${PATH}" bash -c '
  cat >"'${MOCK_DIR}'/aws" <<'"'"'EOF'"'"'
#!/usr/bin/env bash
case "$*" in
  *"stepfunctions list-executions"*) echo "arn:aws:states:us-east-1:123:execution:openci-tf:old" ;;
  *"--query input"*) echo "{\"webhook_info\":{\"trigger_id\":\"other\"}}" ;;
  *"terraform"*) echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf" ;;
  *) echo "unexpected aws call: $*" >&2; exit 97 ;;
esac
EOF
  chmod +x "'${MOCK_DIR}'/aws"
  cd "'${ROOT}'"
  SSM_CONFIG_PROJECT=test-smoke \
  SMOKE_SSM_CONFIG_SCRIPT="'"${MOCK_DIR}"'/scripts/ssm_config.sh" \
  SMOKE_WAIT_EXECUTIONS=1 ./scripts/smoke.sh
'

write_common_mocks
cat >"${MOCK_DIR}/aws" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *"stepfunctions list-executions"*) echo "arn:aws:states:us-east-1:123:execution:openci-tf:old" ;;
  *"stepfunctions describe-execution"*"arn:aws:states:us-east-1:123:execution:openci-tf:old"*)
    if [[ "$*" == *"--query input"* ]]; then echo '{"webhook_info":{"trigger_id":"trigger-smoke","repo_name":"org/repo","pr_number":7,"comment_body":"tf plan infra/vpc"}}';
    elif [[ "$*" == *"--query startDate"* ]]; then echo "2026-08-05T09:58:00Z";
    else echo "SUCCEEDED"; fi
    ;;
  *"terraform -chdir=infra/deploy output -raw state_machine_arn"*)
    echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 97
    ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
expect_rc "smoke rejects stale same-input execution" 1 run_smoke

write_common_mocks
cat >"${MOCK_DIR}/aws" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"stepfunctions list-executions"*)
    if [[ ! -f "${MOCK_DIR}/comment-posted" ]]; then
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old"
    else
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old arn:aws:states:us-east-1:123:execution:openci-tf:new"
    fi
    ;;
  *"stepfunctions describe-execution"*"arn:aws:states:us-east-1:123:execution:openci-tf:new"*)
    if [[ "\$*" == *"--query status"* ]]; then echo "SUCCEEDED";
    elif [[ "\$*" == *"--query input"* ]]; then echo '${GOOD_INPUT}';
    elif [[ "\$*" == *"--query output"* ]]; then echo '${GOOD_OUTPUT}';
    elif [[ "\$*" == *"--query stopDate"* ]]; then echo "2026-08-05T10:00:00Z";
    elif [[ "\$*" == *"--query startDate"* ]]; then echo "2026-08-05T10:00:00Z";
    else echo '{"status":"SUCCEEDED"}'; fi
    ;;
  *"terraform -chdir=infra/deploy output -raw state_machine_arn"*)
    echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
    ;;
  *)
    echo "unexpected aws call: \$*" >&2
    exit 97
    ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
write_gh_comment_marker '  if [[ "$*" == *"updated_at"* ]]; then echo "2026-08-05T09:59:00Z"; exit 0; fi'
expect_rc "smoke rejects stale comment" 1 run_smoke

write_common_mocks
cat >"${MOCK_DIR}/aws" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"stepfunctions list-executions"*)
    if [[ ! -f "${MOCK_DIR}/comment-posted" ]]; then
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old"
    else
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old arn:aws:states:us-east-1:123:execution:openci-tf:new"
    fi
    ;;
  *"stepfunctions describe-execution"*"arn:aws:states:us-east-1:123:execution:openci-tf:new"*)
    echo "AccessDenied" >&2
    exit 254
    ;;
  *"terraform -chdir=infra/deploy output -raw state_machine_arn"*)
    echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
    ;;
  *)
    echo "unexpected aws call: \$*" >&2
    exit 97
    ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
write_gh_comment_marker
expect_rc "smoke propagates aws errors" 1 env PATH="${MOCK_DIR}:${PATH}" SSM_CONFIG_PROJECT=test-smoke SMOKE_SSM_CONFIG_SCRIPT="${MOCK_DIR}/scripts/ssm_config.sh" SMOKE_STARTED_AT="2026-08-05T09:59:30Z" SMOKE_WAIT_EXECUTIONS=1 bash -c 'rm -f "'"${MOCK_DIR}"'/comment-posted" && cd "'"${ROOT}"'" && ./scripts/smoke.sh'

write_common_mocks
cat >"${MOCK_DIR}/aws" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"stepfunctions list-executions"*)
    if [[ ! -f "${MOCK_DIR}/comment-posted" ]]; then
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old"
    else
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old arn:aws:states:us-east-1:123:execution:openci-tf:new"
    fi
    ;;
  *"stepfunctions describe-execution"*"arn:aws:states:us-east-1:123:execution:openci-tf:new"*)
    if [[ "\$*" == *"--query status"* ]]; then echo "SUCCEEDED";
    elif [[ "\$*" == *"--query input"* ]]; then echo '${GOOD_INPUT}';
    elif [[ "\$*" == *"--query output"* ]]; then echo '{}';
    elif [[ "\$*" == *"--query startDate"* ]]; then echo "2026-08-05T10:00:00Z";
    else echo '{"status":"SUCCEEDED"}'; fi
    ;;
  *"terraform -chdir=infra/deploy output -raw state_machine_arn"*)
    echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
    ;;
  *)
    echo "unexpected aws call: \$*" >&2
    exit 97
    ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
write_gh_comment_marker
expect_rc "smoke rejects empty outer output shape" 1 run_smoke

write_common_mocks
cat >"${MOCK_DIR}/aws" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"stepfunctions list-executions"*)
    if [[ ! -f "${MOCK_DIR}/comment-posted" ]]; then
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old"
    else
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old arn:aws:states:us-east-1:123:execution:openci-tf:new"
    fi
    ;;
  *"stepfunctions describe-execution"*"arn:aws:states:us-east-1:123:execution:openci-tf:new"*)
    if [[ "\$*" == *"--query status"* ]]; then echo "SUCCEEDED";
    elif [[ "\$*" == *"--query input"* ]]; then echo '${GOOD_INPUT}';
    elif [[ "\$*" == *"--query output"* ]]; then echo '{"rendered":true,"outcomes":[{"folder":"infra/vpc","output":{"status":"succeeded"}}]}';
    elif [[ "\$*" == *"--query startDate"* ]]; then echo "2026-08-05T10:00:00Z";
    else echo '{"status":"SUCCEEDED"}'; fi
    ;;
  *"terraform -chdir=infra/deploy output -raw state_machine_arn"*)
    echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
    ;;
  *)
    echo "unexpected aws call: \$*" >&2
    exit 97
    ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
write_gh_comment_marker
expect_rc "smoke rejects missing inner execution id" 1 run_smoke

write_common_mocks
cat >"${MOCK_DIR}/aws" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"stepfunctions list-executions"*)
    if [[ ! -f "${MOCK_DIR}/comment-posted" ]]; then
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old"
    else
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old arn:aws:states:us-east-1:123:execution:openci-tf:new"
    fi
    ;;
  *"stepfunctions describe-execution"*"arn:aws:states:us-east-1:123:execution:openci-tf:new"*)
    if [[ "\$*" == *"--query status"* ]]; then echo "SUCCEEDED";
    elif [[ "\$*" == *"--query input"* ]]; then echo '${GOOD_INPUT}';
    elif [[ "\$*" == *"--query output"* ]]; then echo '{"rendered":true,"outcomes":[{"folder":"infra/vpc","output":{"exec_id":"run.folder.0","status":"failed"}}]}';
    elif [[ "\$*" == *"--query startDate"* ]]; then echo "2026-08-05T10:00:00Z";
    else echo '{"status":"SUCCEEDED"}'; fi
    ;;
  *"terraform -chdir=infra/deploy output -raw state_machine_arn"*)
    echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
    ;;
  *)
    echo "unexpected aws call: \$*" >&2
    exit 97
    ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
write_gh_comment_marker
expect_rc "smoke rejects failed inner execution" 1 run_smoke

write_common_mocks
cat >"${MOCK_DIR}/aws" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *"stepfunctions list-executions"*) echo "arn:aws:states:us-east-1:123:execution:openci-tf:old" ;;
  *"--query status"*) echo "FAILED" ;;
  *"--query input"*) echo '{"webhook_info":{"trigger_id":"trigger-smoke","repo_name":"org/repo","pr_number":7,"comment_body":"tf plan infra/vpc"}}' ;;
  *"terraform"*) echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf" ;;
  *) echo "unexpected aws call: $*" >&2; exit 97 ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
expect_rc "smoke fails on non-success terminal status" 1 run_smoke

write_common_mocks
cat >"${MOCK_DIR}/aws" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *"stepfunctions list-executions"*) echo "arn:aws:states:us-east-1:123:execution:openci-tf:old" ;;
  *"terraform"*) echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf" ;;
  *) echo "unexpected aws call: $*" >&2; exit 97 ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
expect_rc "smoke fails on wait timeout" 1 run_smoke

write_common_mocks
cat >"${MOCK_DIR}/aws" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"stepfunctions list-executions"*)
    if [[ ! -f "${MOCK_DIR}/comment-posted" ]]; then
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old"
    else
      echo "arn:aws:states:us-east-1:123:execution:openci-tf:old arn:aws:states:us-east-1:123:execution:openci-tf:new"
    fi
    ;;
  *"stepfunctions describe-execution"*"arn:aws:states:us-east-1:123:execution:openci-tf:new"*)
    if [[ "\$*" == *"--query status"* ]]; then echo "SUCCEEDED";
    elif [[ "\$*" == *"--query input"* ]]; then echo '${GOOD_INPUT}';
    elif [[ "\$*" == *"--query output"* ]]; then echo '${GOOD_OUTPUT}';
    elif [[ "\$*" == *"--query stopDate"* ]]; then echo "2026-08-05T10:00:01Z";
    elif [[ "\$*" == *"--query startDate"* ]]; then echo "2026-08-05T10:00:00Z";
    else echo '{"status":"SUCCEEDED","executionArn":"arn:aws:states:us-east-1:123:execution:openci-tf:new"}'; fi
    ;;
  *"terraform -chdir=infra/deploy output -raw state_machine_arn"*)
    echo "arn:aws:states:us-east-1:123:stateMachine:openci-tf"
    ;;
  *)
    echo "unexpected aws call: \$*" >&2
    exit 97
    ;;
esac
EOF
chmod +x "${MOCK_DIR}/aws"
write_gh_comment_marker '  if [[ "$*" == *"updated_at"* ]]; then echo "2026-08-05T10:00:01Z"; exit 0; fi'
expect_rc "smoke succeeds with correlated execution and comment" 0 run_smoke
