#!/usr/bin/env bash
set -euo pipefail

# Install-time configuration in SSM Parameter Store (SecureString).
# All config lives under /openci-tf/install/<project>/<key>.
#
# Usage:
#   ssm_config.sh set <key> <value>         # non-secret values
#   ssm_config.sh set-stdin <key>           # secret values (read from stdin)
#   ssm_config.sh get <key>                 # fails if unset
#   ssm_config.sh get-or <key> <default>    # default ONLY on ParameterNotFound
#   ssm_config.sh delete-all                # removes the whole namespace
#
# Project namespace defaults to "openci-tf"; override with SSM_CONFIG_PROJECT.
#
# Fail-loud contract: only a genuine ParameterNotFound falls back to the
# default. Expired credentials, AccessDenied, throttling, and network errors
# always abort — silently defaulting on those could flip ownership or naming
# decisions mid-install.

PROJECT="${SSM_CONFIG_PROJECT:-openci-tf}"
PREFIX="/openci-tf/install/${PROJECT}"

usage() {
  grep '^#   ' "$0" | sed 's/^#   //' >&2
  exit 1
}

put_param() { # <key> <value> (non-secret values only)
  aws ssm put-parameter --name "${PREFIX}/$1" --value "$2" \
    --type SecureString --overwrite >/dev/null
  echo "set ${PREFIX}/$1"
}

put_param_from_stdin() { # <key>
  parameter_name="${PREFIX}/$1"
  temp_dir="$(mktemp -d)"
  chmod 700 "$temp_dir"
  value_file="${temp_dir}/value"
  input_file="${temp_dir}/put-parameter.json"
  trap 'rm -rf "$temp_dir"' EXIT
  umask 077
  cat >"$value_file"
  [ -s "$value_file" ] || {
    echo "ERROR: empty value on stdin" >&2
    exit 1
  }
  if [ "$(tail -c 1 "$value_file" | od -An -tx1 | tr -d ' \n')" = "0a" ]; then
    truncate -s -1 "$value_file"
  fi
  [ -s "$value_file" ] || {
    echo "ERROR: empty value after stripping trailing newline" >&2
    exit 1
  }
  jq -Rs --arg name "$parameter_name" \
    '{Name: $name, Value: ., Type: "SecureString", Overwrite: true}' \
    <"$value_file" >"$input_file"
  chmod 600 "$value_file" "$input_file"
  aws ssm put-parameter --cli-input-json "file://${input_file}" >/dev/null
  echo "set ${parameter_name}"
  rm -rf "$temp_dir"
  trap - EXIT
}

cmd="${1:-}"
case "$cmd" in
set)
  key="${2:?set requires <key> <value>}"
  value="${3:?set requires <key> <value>}"
  put_param "$key" "$value"
  ;;
set-stdin)
  key="${2:?set-stdin requires <key>}"
  put_param_from_stdin "$key"
  ;;
get)
  key="${2:?get requires <key>}"
  aws ssm get-parameter --name "${PREFIX}/${key}" --with-decryption \
    --query 'Parameter.Value' --output text
  ;;
get-or)
  key="${2:?get-or requires <key> <default>}"
  default="${3?get-or requires <key> <default>}"
  err_file="$(mktemp)"
  trap 'rm -f "$err_file"' EXIT
  if value="$(aws ssm get-parameter --name "${PREFIX}/${key}" --with-decryption \
    --query 'Parameter.Value' --output text 2>"$err_file")"; then
    echo "$value"
  elif grep -q 'ParameterNotFound' "$err_file"; then
    echo "$default"
  else
    echo "ERROR: SSM get-parameter ${PREFIX}/${key} failed (NOT a missing parameter):" >&2
    cat "$err_file" >&2
    exit 1
  fi
  ;;
delete-all)
  names="$(aws ssm get-parameters-by-path --path "$PREFIX" --recursive \
    --query 'Parameters[].Name' --output text)"
  if [ -z "$names" ] || [ "$names" = "None" ]; then
    echo "no parameters under ${PREFIX}"
    exit 0
  fi
  for name in $names; do
    aws ssm delete-parameter --name "$name"
    echo "deleted $name"
  done
  ;;
*)
  usage
  ;;
esac
