# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts for the operator-known openci-tf Lambda image version."""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_image_version_is_fixed_at_1_01() -> None:
    assert (_REPO_ROOT / "IMAGE_VERSION").read_text(encoding="utf-8") == "1.01\n"
    result = subprocess.run(
        [_REPO_ROOT / "scripts/image_tag.sh"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1.01\n"


def test_deploy_paths_do_not_derive_or_accept_image_tags() -> None:
    justfile = (_REPO_ROOT / "justfile").read_text(encoding="utf-8")

    assert "docker-push tag:" not in justfile
    assert "docker-push:" in justfile
    assert "get-or image_tag" not in justfile
    assert "git rev-parse --short HEAD" not in justfile
    assert justfile.count('IMAGE_TAG="$(./scripts/image_tag.sh)"') == 3


def test_terraform_resolves_fixed_tag_to_current_digest() -> None:
    main = (_REPO_ROOT / "infra/deploy/main.tf").read_text(encoding="utf-8")
    ecr = (_REPO_ROOT / "infra/deploy/modules/ecr/main.tf").read_text(encoding="utf-8")

    assert 'data "aws_ecr_image" "openci_tf"' in main
    assert "image_tag       = var.image_tag" in main
    assert "@${data.aws_ecr_image.openci_tf.image_digest}" in main
    assert main.count("ecr_image_uri                      = local.ecr_image_uri") == 2
    assert "ecr_image_uri              = local.ecr_image_uri" in main
    assert "ecr_image_uri = local.ecr_image_uri" in main
    assert 'image_tag_mutability = "MUTABLE"' in ecr
