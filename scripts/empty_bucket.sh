#!/usr/bin/env bash
set -euo pipefail

# Permanently delete every object version and delete marker in a bucket (or
# under a prefix) so the bucket can be removed / the prefix truly reversed.
# No-ops (successfully) ONLY on a genuine 404; any other head-bucket failure
# (expired STS, AccessDenied) aborts.
#
# Usage: empty_bucket.sh <bucket> [prefix]

BUCKET="${1:?Usage: empty_bucket.sh <bucket> [prefix]}"
PREFIX="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Capture the probe's real exit status (a bare `if !` would lose it: $? inside
# the else branch is the status of `!`, i.e. 0).
set +e
"$SCRIPT_DIR/bucket_exists.sh" "$BUCKET"
probe_rc=$?
set -e
case "$probe_rc" in
0) : ;;
1)
  echo "bucket ${BUCKET} does not exist; nothing to empty"
  exit 0
  ;;
*)
  echo "ERROR: cannot determine whether ${BUCKET} exists (probe rc=${probe_rc}); refusing to continue" >&2
  exit "$probe_rc"
  ;;
esac

PREFIX_ARGS=()
[ -n "$PREFIX" ] && PREFIX_ARGS=(--prefix "$PREFIX")

batch_file="$(mktemp)"
trap 'rm -f "$batch_file"' EXIT

while :; do
  versions="$(aws s3api list-object-versions --bucket "$BUCKET" ${PREFIX_ARGS[@]+"${PREFIX_ARGS[@]}"} --max-items 500 \
    --query '{Objects: [Versions[].{Key:Key,VersionId:VersionId}, DeleteMarkers[].{Key:Key,VersionId:VersionId}][] | [0:500]}' \
    --output json)"
  count="$(printf '%s' "$versions" | jq '.Objects | length')"
  if [ "$count" -eq 0 ]; then
    break
  fi
  printf '%s' "$versions" | jq '{Objects: .Objects, Quiet: true}' >"$batch_file"
  # DeleteObjects exits 0 even when S3 refused some objects: it lists them
  # under Errors. A refused object comes back in the next listing, so stop.
  result="$(aws s3api delete-objects --bucket "$BUCKET" --delete "file://${batch_file}" --output json)"
  failed="$(printf '%s' "$result" | jq -s '[.[].Errors // [] | .[]] | length')"
  if [ "$failed" -ne 0 ]; then
    codes="$(printf '%s' "$result" | jq -rs '[.[].Errors // [] | .[].Code] | unique | join(", ")')"
    echo "ERROR: S3 did not delete ${failed} of ${count} versions from ${BUCKET} (${codes}); keys:" >&2
    printf '%s' "$result" | jq -rs '.[].Errors // [] | .[].Key' >&2
    exit 1
  fi
  echo "deleted ${count} versions from ${BUCKET}"
done

echo "emptied ${BUCKET}${PREFIX:+/${PREFIX}}"
