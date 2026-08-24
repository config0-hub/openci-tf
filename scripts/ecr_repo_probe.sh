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

strip_blank_edges() {
  local text="$1"
  local -a lines=()
  local line start end i result

  if [ -z "$text" ]; then
    printf '%s' ""
    return
  fi

  while IFS= read -r line || [ -n "$line" ]; do
    lines+=("$line")
  done < <(printf '%s' "$text")

  start=0
  end=$((${#lines[@]} - 1))

  while [ "$start" -le "$end" ] && [ -z "${lines[$start]}" ]; do
    start=$((start + 1))
  done
  while [ "$end" -ge "$start" ] && [ -z "${lines[$end]}" ]; do
    end=$((end - 1))
  done

  if [ "$start" -gt "$end" ]; then
    printf '%s' ""
    return
  fi

  result="${lines[$start]}"
  for ((i = start + 1; i <= end; i++)); do
    result="$result"$'\n'"${lines[$i]}"
  done
  printf '%s' "$result"
}

NORMALIZED="$(strip_blank_edges "$OUTPUT")"

if { [ "$RC" -eq 254 ] || [ "$RC" -eq 255 ]; } \
  && [[ "$NORMALIZED" =~ ^An\ error\ occurred\ \(RepositoryNotFoundException\)\ when\ calling\ the\ DescribeRepositories\ operation:\ .+$ ]] \
  && [ "$(printf '%s' "$NORMALIZED" | wc -l | tr -d ' ')" -eq 0 ]; then
  exit 1
fi

printf '%s\n' "$OUTPUT" >&2
exit 2
