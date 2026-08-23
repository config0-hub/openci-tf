#!/usr/bin/env bash
set -euo pipefail

# Upload a COMPLETE copy of the applied Terraform source for one root to the
# state bucket: s3://<bucket>/source/<root_name>/ — single authoritative copy,
# overwritten on every apply (bucket versioning provides history). SSE-S3.
#
# Also writes manifest.json: root, timestamp, terraform version, and variable
# NAMES only (never values). terraform.tfvars itself is NEVER uploaded.
#
# Usage: upload_source.sh <state_bucket> <root_name> <repo_root> <rel_dir> [rel_dir ...]
#   rel_dir paths are relative to <repo_root>; the first is the root itself,
#   extras are shared module directories the root references.

BUCKET="${1:?Usage: upload_source.sh <state_bucket> <root_name> <repo_root> <rel_dir>...}"
ROOT_NAME="${2:?root_name required}"
REPO_ROOT="${3:?repo_root required}"
shift 3
[ $# -ge 1 ] || {
  echo "ERROR: at least one rel_dir required" >&2
  exit 1
}

STAGE="$(mktemp -d)"
LIST_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$LIST_DIR"' EXIT

git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "ERROR: source root is not a Git worktree: $REPO_ROOT" >&2
  exit 1
}
REPO_TOP="$(git -C "$REPO_ROOT" rev-parse --show-toplevel)"
REPO_ROOT_PHYSICAL="$(cd "$REPO_ROOT" && pwd -P)"
REPO_TOP_PHYSICAL="$(cd "$REPO_TOP" && pwd -P)"
[ "$REPO_ROOT_PHYSICAL" = "$REPO_TOP_PHYSICAL" ] || {
  echo "ERROR: source root must be the Git worktree root: $REPO_ROOT" >&2
  exit 1
}
REPO_TOP="$REPO_TOP_PHYSICAL"

NORMALIZED_RELS=()
for rel in "$@"; do
  [ -n "$rel" ] || {
    echo "ERROR: source directory must not be empty" >&2
    exit 1
  }
  requested="${REPO_ROOT}/${rel}"
  [ -d "$requested" ] || {
    echo "ERROR: not a directory: $requested" >&2
    exit 1
  }
  src="$(cd "$requested" && pwd -P)"
  case "$src" in
    "$REPO_TOP") normalized="." ;;
    "$REPO_TOP"/*) normalized="${src#"$REPO_TOP"/}" ;;
    *)
      echo "ERROR: source directory escapes the Git worktree: $rel" >&2
      exit 1
      ;;
  esac
  NORMALIZED_RELS+=("$normalized")
  mkdir -p "$STAGE/$normalized"
  list_file="$(mktemp "$LIST_DIR/tracked.XXXXXX")"
  if [ "$normalized" = "." ]; then
    pathspec=':(top)'
  else
    pathspec=":(top,literal)$normalized"
  fi
  if ! git -C "$REPO_TOP" ls-files -z -- "$pathspec" >"$list_file"; then
    echo "ERROR: could not enumerate tracked source for: $normalized" >&2
    exit 1
  fi
  copied=0
  # STRICT ALLOWLIST: only Git-tracked regular Terraform source and provider
  # lock files are uploaded. Variable values, state, plans, overrides,
  # generated backends, symlinks, and arbitrary untracked files must never
  # reach the versioned source record.
  while IFS= read -r -d '' tracked; do
    case "$tracked" in
      *.tf | *.tf.json | */.terraform.lock.hcl | .terraform.lock.hcl) ;;
      *) continue ;;
    esac
    case "$(basename "$tracked")" in
      backend.tf | override.tf | override.tf.json | *_override.tf | *_override.tf.json) continue ;;
    esac
    source_file="$REPO_TOP/$tracked"
    [ -f "$source_file" ] && [ ! -L "$source_file" ] || {
      echo "ERROR: tracked Terraform source is not a regular file: $tracked" >&2
      exit 1
    }
    mkdir -p "$STAGE/$(dirname "$tracked")"
    cp "$source_file" "$STAGE/$tracked"
    copied=$((copied + 1))
  done <"$list_file"
  [ "$copied" -gt 0 ] || {
    echo "ERROR: no tracked Terraform source found under: $normalized" >&2
    exit 1
  }
  # Regression guard: fail hard if anything value-bearing slipped into the stage.
  if find "$STAGE/$normalized" -type f \( -name '*tfvars*' -o -name '*.tfstate*' -o -name '*.tfplan' \) | grep -q .; then
    echo "ERROR: variable-value or state file staged for upload — aborting" >&2
    exit 1
  fi
done

# Manifest: variable names come from the validated, normalized first root.
ROOT_REL="${NORMALIZED_RELS[0]}"
if [ "$ROOT_NAME" = "console" ]; then
  CONSOLE_LOCK="${ROOT_REL%/}/.terraform.lock.hcl"
  [ "$ROOT_REL" = "." ] && CONSOLE_LOCK=".terraform.lock.hcl"
  git -C "$REPO_TOP" ls-files --error-unmatch -- "$CONSOLE_LOCK" >/dev/null 2>&1 || {
    echo "ERROR: console provider lock is not Git-tracked: $CONSOLE_LOCK" >&2
    exit 1
  }
  [ -f "$STAGE/$CONSOLE_LOCK" ] || {
    echo "ERROR: console provider lock was not staged for source upload: $CONSOLE_LOCK" >&2
    exit 1
  }
fi
TFVARS="${REPO_TOP}/${ROOT_REL}/terraform.tfvars"
VAR_NAMES="[]"
if [ -f "$TFVARS" ]; then
  VAR_NAMES="$(sed -n 's/^\([a-zA-Z0-9_-]*\)[[:space:]]*=.*/\1/p' "$TFVARS" | jq -R . | jq -sc .)"
fi
TF_VERSION="$(terraform version -json | jq -r .terraform_version)"
jq -n \
  --arg root "$ROOT_NAME" \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg terraform_version "$TF_VERSION" \
  --argjson variable_names "$VAR_NAMES" \
  '{root: $root, timestamp: $timestamp, terraform_version: $terraform_version, variable_names: $variable_names}' \
  >"$STAGE/manifest.json"

aws s3 sync "$STAGE" "s3://${BUCKET}/source/${ROOT_NAME}/" --delete --sse AES256 --only-show-errors
echo "uploaded source copy: s3://${BUCKET}/source/${ROOT_NAME}/"
