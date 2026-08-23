#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT}/build"
STAGE_DIR="${BUILD_DIR}/lambda"
ZIP_PATH="${BUILD_DIR}/openci-tf-console.zip"

for required in dist/index.html server-dist/lambda.js package.json package-lock.json; do
  [ -f "${ROOT}/${required}" ] || {
    echo "ERROR: missing ${ROOT}/${required}; run npm run build first" >&2
    exit 1
  }
done

rm -rf "$STAGE_DIR"
rm -f "$ZIP_PATH"
mkdir -p "$STAGE_DIR"
cp -R "${ROOT}/dist" "${ROOT}/server-dist" "$STAGE_DIR/"
cp "${ROOT}/package.json" "${ROOT}/package-lock.json" "$STAGE_DIR/"

# Dev-only modules must never ship: the lambda entry does not import them
# (tsc emits per-file, so exclusion happens here), and mock fixtures behind a
# runtime env var would let one flag flip the authenticated console to fakes.
rm -f "$STAGE_DIR"/server-dist/mock.js "$STAGE_DIR"/server-dist/mock-data.js "$STAGE_DIR"/server-dist/local.js
if grep -rq "acme/payments-infra" "$STAGE_DIR"/server-dist; then
  echo "ERROR: mock fixture content leaked into the lambda package" >&2
  exit 1
fi

# Install only runtime dependencies inside the artifact. Lifecycle scripts are
# unnecessary for this dependency set and are disabled for a reproducible,
# side-effect-free package build.
npm --prefix "$STAGE_DIR" ci --omit=dev --ignore-scripts --no-audit --no-fund

# Normalize mtimes and feed zip a sorted file list. -X strips host-specific
# metadata, so unchanged inputs produce the same archive bytes.
find "$STAGE_DIR" -exec touch -t 198001010000 {} +
(
  cd "$STAGE_DIR"
  LC_ALL=C find . -type f -print | LC_ALL=C sort | zip -X -q "$ZIP_PATH" -@
)

echo "wrote ${ZIP_PATH}"
