#!/usr/bin/env bash
# Return the single checked-in openci-tf Lambda image version used by installation tooling.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${SCRIPT_DIR}/../IMAGE_VERSION"

if [ ! -f "$VERSION_FILE" ]; then
	echo "ERROR: missing checked-in image version: ${VERSION_FILE}" >&2
	exit 1
fi

IMAGE_TAG="$(tr -d '\r\n' <"$VERSION_FILE")"
if [[ ! "$IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
	echo "ERROR: invalid Docker image tag in IMAGE_VERSION" >&2
	exit 1
fi

printf '%s\n' "$IMAGE_TAG"
