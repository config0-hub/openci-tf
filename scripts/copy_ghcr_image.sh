#!/usr/bin/env bash
# Copy the released openci-tf Lambda image from GHCR into the target ECR
# repository. The GHCR reference must be pinned by digest (the digest recorded
# in the GitHub release); the ECR tag is the checked-in IMAGE_VERSION.
set -euo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PROJECT="${OPENCI_TF_PROJECT:-openci-tf}"
GHCR_IMAGE=""

usage() {
  cat >&2 <<'EOF'
Usage: copy_ghcr_image.sh --ghcr-image ghcr.io/<owner>/openci-tf@sha256:<digest> [--region REGION] [--project NAME]

Pulls the digest-pinned GHCR image and pushes it to
<account>.dkr.ecr.<region>.amazonaws.com/<project>:<IMAGE_VERSION>.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage ;;
    --ghcr-image) GHCR_IMAGE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; usage ;;
  esac
done

[[ -n "$GHCR_IMAGE" ]] || { echo "ERROR: --ghcr-image is required" >&2; exit 1; }
case "$GHCR_IMAGE" in
  ghcr.io/*@sha256:*) ;;
  *)
    echo "ERROR: --ghcr-image must be a digest-pinned GHCR reference (ghcr.io/<owner>/openci-tf@sha256:...)" >&2
    exit 1
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="$("${SCRIPT_DIR}/image_tag.sh")"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
ECR_REGISTRY="${ACCT}.dkr.ecr.${REGION}.amazonaws.com"
ECR_IMAGE="${ECR_REGISTRY}/${PROJECT}:${IMAGE_TAG}"

docker pull "$GHCR_IMAGE"
docker tag "$GHCR_IMAGE" "$ECR_IMAGE"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker push "$ECR_IMAGE"
echo "copied ${GHCR_IMAGE} -> ${ECR_IMAGE}"
