#!/usr/bin/env bash
# End-to-end smoke: post a plan comment and verify correlated Step Functions + PR output.
set -euo pipefail

SMOKE_WAIT_EXECUTIONS="${SMOKE_WAIT_EXECUTIONS:-24}"
SMOKE_WAIT_TERMINAL="${SMOKE_WAIT_TERMINAL:-120}"
SMOKE_POLL_SECONDS="${SMOKE_POLL_SECONDS:-10}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

load_config() {
  local ssm_config="${SMOKE_SSM_CONFIG_SCRIPT:-./scripts/ssm_config.sh}"
  PROJECT="${SSM_CONFIG_PROJECT:?set SSM_CONFIG_PROJECT to the smoke config namespace}"
  repo="$(SSM_CONFIG_PROJECT="$PROJECT" "$ssm_config" get repo)"
  pr="$(SSM_CONFIG_PROJECT="$PROJECT" "$ssm_config" get pr_number)"
  folder="$(SSM_CONFIG_PROJECT="$PROJECT" "$ssm_config" get folder)"
  trigger_id="$(SSM_CONFIG_PROJECT="$PROJECT" "$ssm_config" get trigger_id)"
  state_machine="$(terraform -chdir=infra/deploy output -raw state_machine_arn)"
  comment_body="tf plan ${folder}"
}

list_recent_executions() {
  aws stepfunctions list-executions \
    --state-machine-arn "$state_machine" \
    --max-results 10 \
    --query 'executions[].executionArn' \
    --output text
}

collect_baseline_arns() {
  local arn
  for arn in $(list_recent_executions); do
    [[ -n "$arn" && "$arn" != "None" ]] || continue
    echo "$arn"
  done
}

is_baseline_arn() {
  local candidate="$1"
  local baseline
  for baseline in "${BASELINE_ARNS[@]:-}"; do
    [[ "$candidate" == "$baseline" ]] && return 0
  done
  return 1
}

aws_describe_field() {
  local execution_arn="$1"
  local field="$2"
  local value
  if ! value="$(aws stepfunctions describe-execution --execution-arn "$execution_arn" --query "$field" --output text)"; then
    fail "aws stepfunctions describe-execution failed for ${execution_arn} field=${field}"
  fi
  printf '%s' "$value"
}

execution_matches_config() {
  local execution_arn="$1"
  local input
  input="$(aws_describe_field "$execution_arn" input)"
  python3 - "$input" "$trigger_id" "$repo" "$pr" "$comment_body" "$folder" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
webhook = payload.get("webhook_info") or {}
required = {
    "trigger_id": sys.argv[2],
    "repo_name": sys.argv[3],
    "pr_number": int(sys.argv[4]),
    "comment_body": sys.argv[5],
}
for key, value in required.items():
    if webhook.get(key) != value:
        raise SystemExit(1)
folders = payload.get("folders") or []
if folders and sys.argv[6] not in folders:
    raise SystemExit(1)
PY
}

execution_started_after_smoke() {
  local execution_arn="$1"
  local start_date
  start_date="$(aws_describe_field "$execution_arn" startDate)"
  python3 - "$start_date" "$SMOKE_STARTED_AT" <<'PY'
import sys
from datetime import datetime
start = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
marker = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
raise SystemExit(0 if start >= marker else 1)
PY
}

find_correlated_execution() {
  local candidate
  for candidate in $(list_recent_executions); do
    [[ -n "$candidate" && "$candidate" != "None" ]] || continue
    is_baseline_arn "$candidate" && continue
    execution_matches_config "$candidate" || continue
    execution_started_after_smoke "$candidate" || continue
    echo "$candidate"
    return 0
  done
  return 1
}

wait_for_correlated_execution() {
  local attempt candidate=""
  for attempt in $(seq 1 "$SMOKE_WAIT_EXECUTIONS"); do
    if candidate="$(find_correlated_execution)"; then
      echo "$candidate"
      return 0
    fi
    sleep 5
  done
  fail "no new correlated openci-tf execution observed for trigger=${trigger_id} repo=${repo} pr=${pr} folder=${folder}"
}

wait_for_terminal_status() {
  local execution_arn="$1"
  local status attempt
  for attempt in $(seq 1 "$SMOKE_WAIT_TERMINAL"); do
    status="$(aws_describe_field "$execution_arn" status)"
    echo "status=${status}" >&2
    [[ "$status" == "RUNNING" ]] || {
      echo "$status"
      return 0
    }
    sleep "$SMOKE_POLL_SECONDS"
  done
  fail "execution timed out: ${execution_arn}"
}

verify_successful_execution() {
  local execution_arn="$1"
  local status output
  status="$(aws_describe_field "$execution_arn" status)"
  [[ "$status" == "SUCCEEDED" ]] || fail "outer execution did not succeed: status=${status} arn=${execution_arn}"
  output="$(aws_describe_field "$execution_arn" output)"
  [[ -n "$output" && "$output" != "None" ]] || fail "outer execution succeeded without output: ${execution_arn}"
  echo "$output"
}

verify_outer_payload() {
  local output="$1"
  python3 - "$output" "$folder" <<'PY'
import json, sys
payload = json.loads(sys.argv[1])
if payload.get("rendered") is not True:
    raise SystemExit("outer output missing rendered=true")
outcomes = payload.get("outcomes")
if not isinstance(outcomes, list) or not outcomes:
    raise SystemExit("outer output missing outcomes")
folder = sys.argv[2]
match = next((item for item in outcomes if item.get("folder") == folder), None)
if not isinstance(match, dict):
    raise SystemExit(f"outer output missing folder outcome for {folder}")
inner = match.get("output") or {}
exec_id = inner.get("exec_id") or inner.get("execution_id") or match.get("execution_id")
if not exec_id:
    raise SystemExit("outer output missing inner execution id")
status = inner.get("status") or inner.get("succeeded")
if status not in ("succeeded", True):
    raise SystemExit(f"inner execution not successful: {status!r}")
print(f"inner_exec_id={exec_id}")
print(f"inner_status={status}")
PY
}

latest_openci_tf_comment_epoch() {
  local marker="<!-- openci-tf:"
  gh api "repos/${repo}/issues/${pr}/comments" --paginate \
    --jq "[.[] | select(.body | contains(\"${marker}\"))] | last | .created_at" 2>/dev/null
}

verify_pr_comment() {
  local execution_arn="$1"
  local marker="<!-- openci-tf:"
  local url updated_at smoke_started
  url="$(gh api "repos/${repo}/issues/${pr}/comments" --paginate --jq "[.[] | select(.body | contains(\"${marker}\"))] | max_by(.updated_at) | .html_url")"
  [[ -n "$url" && "$url" != "null" ]] || fail "no rendered openci-tf PR comment found for ${repo}#${pr}"
  updated_at="$(gh api "repos/${repo}/issues/${pr}/comments" --paginate --jq "[.[] | select(.body | contains(\"${marker}\"))] | max_by(.updated_at) | .updated_at")"
  smoke_started="${SMOKE_STARTED_AT:?}"
  python3 - "$updated_at" "$smoke_started" <<'PY'
import sys
from datetime import datetime
updated = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
started = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
if updated < started:
    raise SystemExit("openci-tf comment predates smoke start")
PY
  echo "comment_url=${url}"
}

main() {
  require_cmd aws
  require_cmd gh
  require_cmd terraform
  require_cmd python3
  local root
  root="$(cd "$(dirname "$0")/.." && pwd)"
  load_config
  BASELINE_ARNS=()
  while IFS= read -r arn; do
    [[ -n "$arn" ]] || continue
    BASELINE_ARNS+=("$arn")
  done < <(collect_baseline_arns)
  SMOKE_STARTED_AT="${SMOKE_STARTED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
  local execution status output
  gh pr comment "$pr" --repo "$repo" --body "$comment_body"
  execution="$(wait_for_correlated_execution)"
  echo "execution=${execution}"
  status="$(wait_for_terminal_status "$execution")"
  [[ "$status" == "SUCCEEDED" ]] || fail "outer execution ended non-success: status=${status} arn=${execution}"
  output="$(verify_successful_execution "$execution")"
  verify_outer_payload "$output"
  verify_pr_comment "$execution"
  aws stepfunctions describe-execution --execution-arn "$execution" \
    --query '{status:status,executionArn:executionArn,startDate:startDate,stopDate:stopDate}' --output json
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  cd "$(dirname "$0")/.."
  main "$@"
fi
