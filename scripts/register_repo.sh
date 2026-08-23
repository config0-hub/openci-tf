#!/usr/bin/env bash
#
# Register a repository in the openci-tf-settings DynamoDB table.
#
# Usage:
#   ./register_repo.sh \
#     --trigger-id <trigger_id> \
#     --repo-name <org/repo> \
#     --git-url <https://github.com/org/repo.git> \
#     --webhook-secret-ssm <ssm_path> \
#     --github-token-ssm <ssm_path> \
#     --upstream-urls-json <json_object> \
#     [--github-capability-pr-number <existing_pr>] \
#     [--github-capability-collaborator <github_user>] \
#     [--region <aws_region>] \
#     [--table <table_name>]

set -euo pipefail

REGION="us-east-1"
TABLE="openci-tf-settings"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) sed -n '1,17p' "$0"; exit 0 ;;
    --trigger-id)         TRIGGER_ID="$2"; shift 2 ;;
    --repo-name)          REPO_NAME="$2"; shift 2 ;;
    --git-url)            GIT_URL="$2"; shift 2 ;;
    --webhook-secret-ssm) WEBHOOK_SECRET_SSM="$2"; shift 2 ;;
    --github-token-ssm)   GITHUB_TOKEN_SSM="$2"; shift 2 ;;
    --upstream-urls-json) UPSTREAM_URLS_JSON="$2"; shift 2 ;;
    --infracost-api-key-ssm) INFRACOST_API_KEY_SSM="$2"; shift 2 ;;
    --require-approval) REQUIRE_APPROVAL="true"; shift ;;
    --github-capability-pr-number) GITHUB_CAPABILITY_PR_NUMBER="$2"; shift 2 ;;
    --github-capability-collaborator) GITHUB_CAPABILITY_COLLABORATOR="$2"; shift 2 ;;
    --region)             REGION="$2"; shift 2 ;;
    --table)              TABLE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

for var in TRIGGER_ID REPO_NAME GIT_URL WEBHOOK_SECRET_SSM GITHUB_TOKEN_SSM UPSTREAM_URLS_JSON; do
  if [[ -z "${!var:-}" ]]; then
    echo "Error: --${var,,} is required" | sed 's/_/-/g'
    exit 1
  fi
done

if ! python3 - "$GITHUB_TOKEN_SSM" <<'PY'
import sys
from src.platform.aws.clone_token import validate_clone_token_path
validate_clone_token_path(sys.argv[1])
PY
then
  echo "Error: --github-token-ssm must be a valid /openci-tf/clone-token/ path" >&2
  exit 1
fi

if ! python3 - "$GIT_URL" "$REPO_NAME" <<'PY'
import sys
from src.platform.git.origin import validate_clone_source
validate_clone_source(sys.argv[1], sys.argv[2])
PY
then
  echo "Error: --git-url must be the canonical HTTPS URL for --repo-name" >&2
  exit 1
fi

if ! python3 - "$UPSTREAM_URLS_JSON" <<'PY'
import json
import sys
from src.domain.cmd_builder.installers import PINNED_UPSTREAM_URLS

raw = json.loads(sys.argv[1])
if not isinstance(raw, dict) or not raw:
    raise SystemExit("upstream-urls-json must be a non-empty JSON object")
for key, value in raw.items():
    if key not in PINNED_UPSTREAM_URLS:
        supported = ", ".join(sorted(PINNED_UPSTREAM_URLS))
        raise SystemExit(f"upstream-urls-json key {key!r} is not a pinned binary:version key; supported keys: {supported}")
    if not isinstance(value, str) or not value.startswith("https://"):
        raise SystemExit(f"upstream-urls-json[{key!r}] must be an https URL")
PY
then
  echo "Error: --upstream-urls-json must be a JSON object of pinned binary:version=https:// download URLs" >&2
  exit 1
fi

verifier_args=(--repo "$REPO_NAME" --token-stdin)
if [[ -n "${GITHUB_CAPABILITY_PR_NUMBER:-}" ]]; then
  verifier_args+=(--github-capability-pr-number "$GITHUB_CAPABILITY_PR_NUMBER")
fi
if [[ -n "${GITHUB_CAPABILITY_COLLABORATOR:-}" ]]; then
  verifier_args+=(--github-capability-collaborator "$GITHUB_CAPABILITY_COLLABORATOR")
fi
if ! aws ssm get-parameter \
    --region "$REGION" \
    --name "$GITHUB_TOKEN_SSM" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text \
    | python3 -m src.platform.github.capability_verifier "${verifier_args[@]}"; then
  echo "Error: GitHub control token capability verification failed; repository was not registered" >&2
  exit 1
fi

item_json="$(jq -n \
  --arg pk "repo" \
  --arg sk "$TRIGGER_ID" \
  --arg repo_name "$REPO_NAME" \
  --arg git_url "$GIT_URL" \
  --arg webhook_secret_ssm "$WEBHOOK_SECRET_SSM" \
  --arg ssm_openci_tf_github_token "$GITHUB_TOKEN_SSM" \
  --arg aws_default_region "$REGION" \
  --argjson upstream_urls "$UPSTREAM_URLS_JSON" \
  --arg ssm_infracost_api_key "${INFRACOST_API_KEY_SSM:-}" \
  --argjson require_approval "${REQUIRE_APPROVAL:-false}" \
  '{
    pk: {S: $pk},
    sk: {S: $sk},
    repo_name: {S: $repo_name},
    git_url: {S: $git_url},
    webhook_secret_ssm: {S: $webhook_secret_ssm},
    ssm_openci_tf_github_token: {S: $ssm_openci_tf_github_token},
    aws_default_region: {S: $aws_default_region},
    upstream_urls: {M: ($upstream_urls | to_entries | map({(.key): {S: .value}}) | add)}
  } + (if $ssm_infracost_api_key != "" then {ssm_infracost_api_key: {S: $ssm_infracost_api_key}} else {} end)
    + (if $require_approval then {require_approval: {BOOL: true}} else {} end)')"

aws dynamodb put-item \
  --region "$REGION" \
  --table-name "$TABLE" \
  --item "$item_json"

echo "Registered $REPO_NAME with trigger_id=$TRIGGER_ID"
echo "Webhook URL: <api-gateway-url>/webhook/$TRIGGER_ID"
