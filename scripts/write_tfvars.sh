#!/usr/bin/env bash
set -euo pipefail

# Write terraform.tfvars into a terraform root from key=value arguments.
# Values that look like JSON arrays/objects are written raw; every other value
# is quoted with HCL escaping. Terraform converts a quoted "7" or "true" to a
# typed number or bool variable, while a raw 1.10 is the number 1.1.
# The file is gitignored — it may hold values read from SSM and must never be
# committed or uploaded.
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
  \[* | \{*)
    printf '%s = %s\n' "$key" "$value" >>"$OUT"
    ;;
  *)
    escaped="$(hcl_escape_string "$value")"
    printf '%s = "%s"\n' "$key" "$escaped" >>"$OUT"
    ;;
  esac
done

echo "wrote ${OUT} ($# variables)"
