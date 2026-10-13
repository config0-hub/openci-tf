# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for safe tfvars generation."""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRITE_TFVARS = _REPO_ROOT / "scripts/write_tfvars.sh"


def test_write_tfvars_escapes_hcl_injection(tmp_path):
    root = tmp_path / "tf"
    root.mkdir()
    malicious = 'evil" bucket = "pwned'
    subprocess.run(
        [_WRITE_TFVARS, str(root), f"state_bucket_arn={malicious}"],
        cwd=_REPO_ROOT,
        check=True,
    )
    content = (root / "terraform.tfvars").read_text()
    assert 'state_bucket_arn = "evil\\" bucket = \\"pwned"' in content
    assert content.count("bucket =") == 1


def test_write_tfvars_quotes_numeric_looking_strings(tmp_path):
    # HCL reads a raw 1.10 as the number 1.1; a quoted "1.10" stays "1.10" and
    # Terraform still converts quoted "7" / "true" to number / bool variables.
    root = tmp_path / "tf"
    root.mkdir()
    subprocess.run(
        [
            _WRITE_TFVARS,
            str(root),
            "image_tag=1.10",
            "tmp_lifecycle_days=7",
            "enabled=true",
            'target_account_ids=["1"]',
        ],
        cwd=_REPO_ROOT,
        check=True,
    )
    content = (root / "terraform.tfvars").read_text()
    assert 'image_tag = "1.10"\n' in content
    assert 'tmp_lifecycle_days = "7"\n' in content
    assert 'enabled = "true"\n' in content
    assert 'target_account_ids = ["1"]\n' in content
