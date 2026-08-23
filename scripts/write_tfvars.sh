#!/usr/bin/env bash
set -euo pipefail

# Write terraform.tfvars into a terraform root from key=value arguments.
# Values that look like JSON arrays/objects, numbers, or booleans are written
# raw; everything else is quoted with HCL escaping. The file is gitignored —
# it may hold values read from SSM and must never be committed or uploaded.
#
# Usage: write_tfvars.sh <root_dir> [key=value ...]

ROOT_DIR="${1:?Usage: write_tfvars.sh <root_dir> [key=value ...]}"
shift

[ -d "$ROOT_DIR" ] || {
  echo "ERROR: not a directory: $ROOT_DIR" >&2
  exit 1
}

hcl_escape_string() {
  # Escape backslashes and double quotes for HCL double-quoted strings.
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

OUT="${ROOT_DIR}/terraform.tfvars"
: >"$OUT"

for pair in "$@"; do
  key="${pair%%=*}"
  value="${pair#*=}"
  if [ -z "$key" ] || [ "$key" = "$pair" ]; then
    echo "ERROR: argument is not key=value: $pair" >&2
    exit 1
  fi
  case "$value" in
  \[* | \{* | true | false)
    printf '%s = %s\n' "$key" "$value" >>"$OUT"
    ;;
  *)
    if printf '%s' "$value" | grep -Eq '^-?[0-9]+(\.[0-9]+)?$'; then
      printf '%s = %s\n' "$key" "$value" >>"$OUT"
    else
      escaped="$(hcl_escape_string "$value")"
      printf '%s = "%s"\n' "$key" "$escaped" >>"$OUT"
    fi
    ;;
  esac
done

echo "wrote ${OUT} ($# variables)"
