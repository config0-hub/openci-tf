#!/usr/bin/env bash

product_log_group_names() {
  local p="$1"
  local hub_lambdas=(
    trigger-stepf parse-command validate-and-resolve render-pr api
    intent-create intent-confirm finalize-run console
  )
  local engine_lambdas=(init-job worker finalizer)
  local run_folder_handlers=(
    prepare-and-submit poll-done collect persist-retry-attempt write-failure-manifest
  )
  local lane_suffixes=("" -apply -destroy)
  local lane handler suffix name

  for name in "${hub_lambdas[@]}"; do
    printf '/aws/lambda/%s-%s\n' "$p" "$name"
  done
  for name in "${engine_lambdas[@]}"; do
    printf '/aws/lambda/%s-%s\n' "$p" "$name"
  done
  printf '/aws/codebuild/%s-worker\n' "$p"
  for suffix in "${lane_suffixes[@]}"; do
    for handler in "${run_folder_handlers[@]}"; do
      printf '/aws/lambda/%s-run-folder%s-%s\n' "$p" "$suffix" "$handler"
    done
    printf '/aws/vendedlogs/states/%s-run-folder%s\n' "$p" "$suffix"
  done
  printf '/aws/vendedlogs/states/%s\n' "$p"
  printf '/aws/vendedlogs/states/%s-apply\n' "$p"
  printf '/aws/vendedlogs/states/%s-destroy\n' "$p"
}
