# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural state-address compatibility for pre-split executor role upgrades.

These tests compare checked-in pre-split state schema fixtures to current Terraform
resource addresses. They do NOT load state into terraform plan — see fixture names
and docstrings; reviewers should not treat this as state-backed plan coverage.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO_ROOT / "tests/fixtures"
_HUB_SETUP = _REPO_ROOT / "infra/modules/hub-setup"
_TARGET_CONNECT_MAIN = _REPO_ROOT / "infra/target-connect/main.tf"


def _terraform_resource_addresses(root: Path, module_prefix: str = "") -> set[str]:
    addresses: set[str] = set()
    pattern = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"')
    for tf_file in root.rglob("*.tf"):
        if ".terraform" in tf_file.parts:
            continue
        for line in tf_file.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if match:
                resource_type, name = match.groups()
                prefix = f"{module_prefix}." if module_prefix else ""
                addresses.add(f"{prefix}{resource_type}.{name}")
    return addresses


def _fixture_addresses(fixture_name: str) -> list[str]:
    data = json.loads((_FIXTURES / fixture_name).read_text(encoding="utf-8"))
    addresses: list[str] = []
    for resource in data.get("resources", []):
        module = resource.get("module", "")
        if module and not module.endswith("."):
            module = f"{module}."
        addresses.append(
            f"{module}{resource['type']}.{resource['name']}"
        )
    return addresses


def test_hub_pre_split_state_addresses_remain_in_hub_setup_config() -> None:
    """Pre-split hub deploy state must still map to hub-setup resource addresses."""
    current = _terraform_resource_addresses(_HUB_SETUP, module_prefix="module.hub_setup")
    missing = [addr for addr in _fixture_addresses("pre_split_hub_deploy_state.json") if addr not in current]
    assert not missing, f"legacy hub addresses missing from hub-setup: {missing}"


def test_target_connect_pre_split_state_addresses_remain_in_config() -> None:
    """Pre-split target-connect state must still map to target-connect module addresses."""
    module_root = _REPO_ROOT / "infra/modules/target-connect"
    current = _terraform_resource_addresses(module_root, module_prefix="module.target_connect")
    missing = [
        addr
        for addr in _fixture_addresses("pre_split_target_connect_state.json")
        if addr not in current
    ]
    assert not missing, f"legacy remote addresses missing from target-connect module: {missing}"


def test_target_connect_root_declares_readonly_alongside_legacy_module() -> None:
    root = _TARGET_CONNECT_MAIN.read_text(encoding="utf-8")
    assert 'module "target_connect"' in root
    assert 'module "executor_readonly"' in root
    assert "moved {" not in root
