#!/usr/bin/env bash
# Validate Terraform syntax for executor-role modules and target-connect roots.
# Uses -backend=false init so no remote state or network is required when the
# plugin cache is warm.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

ROOTS=(
  infra/modules/hub-setup
  infra/modules/executor-readonly
  infra/modules/executor-poweruser
  infra/target-connect
  infra/target-connect-poweruser
  infra/deploy
)

terraform_binary() {
  if command -v terraform >/dev/null 2>&1; then
    printf '%s\n' terraform
  elif command -v tofu >/dev/null 2>&1; then
    printf '%s\n' tofu
  else
    echo "ERROR: neither terraform nor tofu is installed" >&2
    return 1
  fi
}

TF_BIN="$(terraform_binary)"
FAILURES=0
for root in "${ROOTS[@]}"; do
  echo "==> ${TF_BIN} fmt -check ${root}"
  if ! "$TF_BIN" -chdir="$root" fmt -check -recursive; then
    echo "FAIL fmt ${root}"
    FAILURES=$((FAILURES + 1))
    continue
  fi
  deploy_data_dir=""
  if [ "$root" = "infra/deploy" ]; then
    deploy_data_dir="$(mktemp -d)"
    export TF_DATA_DIR="$deploy_data_dir"
  fi
  echo "==> ${TF_BIN} init -backend=false ${root}"
  if ! "$TF_BIN" -chdir="$root" init -backend=false -input=false >/dev/null; then
    echo "FAIL init ${root}"
    FAILURES=$((FAILURES + 1))
    if [ -n "$deploy_data_dir" ]; then
      unset TF_DATA_DIR
      rm -rf "$deploy_data_dir"
    fi
    continue
  fi
  echo "==> ${TF_BIN} validate ${root}"
  if ! "$TF_BIN" -chdir="$root" validate; then
    echo "FAIL validate ${root}"
    FAILURES=$((FAILURES + 1))
  fi
  if [ -n "$deploy_data_dir" ]; then
    unset TF_DATA_DIR
    rm -rf "$deploy_data_dir"
  fi
done

if [ "$FAILURES" -gt 0 ]; then
  echo "validate_terraform.sh: ${FAILURES} failure(s)"
  exit 1
fi
echo "validate_terraform.sh: all roots passed"
