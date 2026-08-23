#!/usr/bin/env bash
# Exact tri-state ECR repository probe: 0 present, 1 exact absence, 2 indeterminate.
set -euo pipefail

REPOSITORY="${1:?Usage: ecr_repo_probe.sh <repository> <region>}"
REGION="${2:?Usage: ecr_repo_probe.sh <repository> <region>}"

set +e
OUTPUT="$(aws ecr describe-repositories \
  --region "$REGION" \
  --repository-names "$REPOSITORY" \
  --query 'repositories[0].repositoryName' \
  --output text 2>&1)"
RC=$?
set -e

if [ "$RC" -eq 0 ] && [ "$OUTPUT" = "$REPOSITORY" ]; then
  exit 0
fi

if [ "$RC" -eq 254 ] \
  && [[ "$OUTPUT" =~ ^An\ error\ occurred\ \(RepositoryNotFoundException\)\ when\ calling\ the\ DescribeRepositories\ operation:\ .+$ ]] \
  && [ "$(printf '%s' "$OUTPUT" | wc -l | tr -d ' ')" -eq 0 ]; then
  exit 1
fi

printf '%s\n' "$OUTPUT" >&2
exit 2
