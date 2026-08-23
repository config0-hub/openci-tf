#!/usr/bin/env bash
# Store the repository-scoped GitHub control PAT in SSM SecureString.
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
TOKEN_FILE="-"

usage() {
  cat >&2 <<'EOF'
Usage: install_github_control_token.sh --repo ORG/REPO [--token-file FILE|-] [--region REGION]

Reads the token from FILE or stdin and writes it to /openci-tf/clone-token/<ORG-REPO-control>
as a SecureString. The token value is never accepted as an argv argument.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --repo) REPO_NAME="$2"; shift 2 ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

: "${REPO_NAME:?--repo is required}"

if ! python3 - "$REPO_NAME" <<'PY'
import re
import sys
repo = sys.argv[1]
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
    raise SystemExit(1)
if ".." in repo or repo.startswith(("-", ".")) or repo.endswith(("-", ".")):
    raise SystemExit(1)
PY
then
  echo "ERROR: --repo must be exactly ORG/REPO" >&2
  exit 1
fi

repo_leaf="${REPO_NAME//\//-}-control"
ssm_path="/openci-tf/clone-token/${repo_leaf}"

if [[ "$TOKEN_FILE" == "-" ]]; then
  token_tmp="$(mktemp)"
  trap 'rm -f "$token_tmp"' EXIT
  chmod 600 "$token_tmp"
  cat >"$token_tmp"
  [[ -s "$token_tmp" ]] || { echo "ERROR: empty token" >&2; exit 1; }
  token_value_arg="file://${token_tmp}"
else
  [[ -r "$TOKEN_FILE" ]] || { echo "ERROR: token file is not readable" >&2; exit 1; }
  [[ -s "$TOKEN_FILE" ]] || { echo "ERROR: empty token file" >&2; exit 1; }
  token_value_arg="file://${TOKEN_FILE}"
fi

aws ssm put-parameter \
  --region "$REGION" \
  --name "$ssm_path" \
  --value "$token_value_arg" \
  --type SecureString \
  --overwrite >/dev/null

printf 'stored GitHub control token at %s\n' "$ssm_path"
