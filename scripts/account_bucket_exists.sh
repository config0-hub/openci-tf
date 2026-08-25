#!/usr/bin/env bash

# Global engine bucket names (openci-tf-internal, openci-tf-done) live only in the
# hub account. head-bucket from another account returns 403 Forbidden, not 404.
# list-buckets scopes the probe to buckets owned by this account.
account_bucket_exists() {
  local name="$1" found err
  err="$(mktemp)"
  if ! found="$(aws s3api list-buckets --query "Buckets[?Name=='${name}'].Name" --output text 2>"$err")"; then
    cat "$err" >&2
    rm -f "$err"
    return 254
  fi
  rm -f "$err"
  if [ -n "$found" ] && [ "$found" != "None" ]; then
    return 0
  fi
  echo "NoSuchBucket: ${name} not in this account" >&2
  return 1
}
