"""Fresh mocked hub-setup plans assert executor address stability only.

These tests do not load Terraform state or assert pre-split plan actions; see
tests/unit/test_state_address_compatibility.py for structural address fixtures.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HUB_SETUP = _REPO_ROOT / "infra/modules/hub-setup"


def test_hub_setup_fresh_plan_preserves_executor_local_address() -> None:
    """Fresh mocked plan — not a pre-split state import; asserts address stability only."""
    result = subprocess.run(
        [
            "terraform",
            f"-chdir={_HUB_SETUP}",
            "test",
            "-filter=tests/legacy_upgrade.tftest.hcl",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_executor_poweruser_policy_renders_under_terraform_test() -> None:
    result = subprocess.run(
        [
            "terraform",
            f"-chdir={_REPO_ROOT / 'infra/modules/executor-poweruser'}",
            "test",
            "-filter=tests/policy_render.tftest.hcl",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
