#!/usr/bin/env bash
# Create a GitHub repository webhook without placing its secret on the command line.
set -euo pipefail
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) echo 'Usage: create_webhook.sh --repo ORG/REPO --webhook-url URL --secret-ssm PATH --github-token-ssm PATH [--region REGION]'; exit 0 ;;
    --repo) REPO="$2"; shift 2 ;; --webhook-url) WEBHOOK_URL="$2"; shift 2 ;;
    --secret-ssm) SECRET_SSM="$2"; shift 2 ;; --github-token-ssm) TOKEN_SSM="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;; *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done
for var in REPO WEBHOOK_URL SECRET_SSM TOKEN_SSM; do [[ -n "${!var:-}" ]] || { echo "Error: --${var,,} is required" | sed 's/_/-/g'; exit 1; }; done
REGION="${REGION:-us-east-1}"
secret=$(aws ssm get-parameter --with-decryption --name "$SECRET_SSM" --region "$REGION" --query 'Parameter.Value' --output text)
token=$(aws ssm get-parameter --with-decryption --name "$TOKEN_SSM" --region "$REGION" --query 'Parameter.Value' --output text)
curl --fail-with-body --silent --show-error -X POST "https://api.github.com/repos/$REPO/hooks" -H "Authorization: Bearer $token" -H 'Accept: application/vnd.github+json' -d "{\"name\":\"web\",\"active\":true,\"events\":[\"issue_comment\",\"pull_request\"],\"config\":{\"url\":\"$WEBHOOK_URL\",\"content_type\":\"json\",\"secret\":\"$secret\",\"insecure_ssl\":\"0\"}}"
