#!/usr/bin/env bash
set -euo pipefail

# Canonical target-role ExternalId derivation.
# Usage: derive_external_id.sh <12-digit-hub-account-id> <12-digit-target-account-id>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" exec "$PYTHON_BIN" -m src.domain.accounts.external_id "$@"
