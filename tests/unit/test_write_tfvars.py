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
