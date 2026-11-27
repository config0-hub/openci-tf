#!/usr/bin/env bash

# Where the engine's infra/02-deploy Terraform state lives in the state bucket.
# engine_install.sh and engine_uninstall.sh both source this file, so install
# and uninstall read the same key. Older installs kept the state under the
# legacy key; engine_install.sh migrates it to the canonical key.
CANONICAL_STATE_KEY="engine/terraform.tfstate"
LEGACY_STATE_KEY="engine-02-deploy/terraform.tfstate"

# state_object_status <bucket> <key>: print present or absent. Any other
# head-object failure (AccessDenied, expired credentials) prints the error and
# returns 1.
state_object_status() {
  local bucket="$1" key="$2" error_file rc
  error_file="$(mktemp)"
  set +e
  aws s3api head-object --bucket "$bucket" --key "$key" >/dev/null 2>"$error_file"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    rm -f "$error_file"
    printf 'present'
    return 0
  fi
  if grep -Eq '(404|Not Found|NoSuchKey)' "$error_file"; then
    rm -f "$error_file"
    printf 'absent'
    return 0
  fi
  cat "$error_file" >&2
  rm -f "$error_file"
  echo "ERROR: could not determine state object status for s3://${bucket}/${key}" >&2
  return 1
}

# engine_state_key_choice <bucket>: print which key holds the engine state.
#   canonical  the canonical object exists, or neither object exists
#   legacy     only the legacy object exists
#   ambiguous  both objects exist
# Returns 1 when a probe fails. Assign the output to a variable before you test
# it: errexit does not see a failed command substitution inside case or [ ].
engine_state_key_choice() {
  local bucket="$1" canonical_status legacy_status
  canonical_status="$(state_object_status "$bucket" "$CANONICAL_STATE_KEY")" || return 1
  legacy_status="$(state_object_status "$bucket" "$LEGACY_STATE_KEY")" || return 1
  if [ "$canonical_status" = present ] && [ "$legacy_status" = present ]; then
    printf 'ambiguous'
  elif [ "$legacy_status" = present ]; then
    printf 'legacy'
  else
    printf 'canonical'
  fi
}
