# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Terraform validate checks for executor-role modules and roots."""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VALIDATE_SCRIPT = _REPO_ROOT / "scripts/validate_terraform.sh"


def test_validate_terraform_script_exists():
    assert _VALIDATE_SCRIPT.is_file()
    assert _VALIDATE_SCRIPT.stat().st_mode & 0o111


def test_validate_terraform_runs_locally():
    result = subprocess.run(
        ["bash", str(_VALIDATE_SCRIPT)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
