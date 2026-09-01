#!/usr/bin/env bash
# Destroy engine infra/02-deploy when the engine checkout has no justfile uninstall recipe.
set -euo pipefail

ENGINE_ROOT="${1:?Usage: engine_uninstall.sh <engine_repo_path>}"
PROJECT_PREFIX="${OPENCI_TF_PROJECT:-openci-tf}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="${PROJECT_PREFIX}-state-${ACCT}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE_ROOT="$(cd "$ENGINE_ROOT" && pwd)"
DEPLOY_DIR="${ENGINE_ROOT}/infra/02-deploy"
ECR_DIR="${ENGINE_ROOT}/infra/01-ecr"

if ! command -v tofu >/dev/null 2>&1; then
  echo "ERROR: tofu is required but not found in PATH" >&2
  exit 1
fi

if [ ! -d "$DEPLOY_DIR" ]; then
  echo "ERROR: engine checkout missing infra/02-deploy: ${ENGINE_ROOT}" >&2
  exit 1
fi

"${ROOT_DIR}/scripts/generate_backend.sh" "$STATE_BUCKET" engine "$REGION" "$DEPLOY_DIR"
(
  cd "$DEPLOY_DIR"
  if [ -f terraform.tfvars ]; then
    terraform init -reconfigure -input=false -backend-config=use_lockfile=true
    terraform destroy -input=false -auto-approve
  else
    echo "no engine terraform.tfvars; skipping destroy"
  fi
)

if [ ! -d "$ECR_DIR" ]; then
  echo "ERROR: engine checkout missing infra/01-ecr: ${ENGINE_ROOT}" >&2
  exit 1
fi

"${ROOT_DIR}/scripts/generate_backend.sh" "$STATE_BUCKET" engine-ecr "$REGION" "$ECR_DIR"
(
  cd "$ECR_DIR"
  if [ -f terraform.tfvars ]; then
    tofu init -reconfigure -input=false -backend-config=use_lockfile=true
    tofu destroy -input=false -auto-approve
  else
    echo "no engine ecr terraform.tfvars; skipping destroy"
  fi
)

echo "engine uninstall complete"
