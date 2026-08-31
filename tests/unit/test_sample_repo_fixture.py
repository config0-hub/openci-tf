# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the repository snapshot used by the sample repository fixture."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from src.domain.config.outer_state import discover_folders, resolve_outer_state

_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures/sample-repo/sample-target-repo"
)
_SNAPSHOT_PATH = _FIXTURE_ROOT.with_suffix(".snapshot.json")
_COVERED_FOLDERS = {
    "terraform/ap-northeast-1/01-vpc": {
        "account_alias": "primary",
        "account_id": "111111111111",
        "region": "ap-northeast-1",
    },
    "terraform/target-us-east-1/01-vpc": {
        "account_alias": "remote",
        "account_id": "222222222222",
        "region": "us-east-1",
    },
}
_UPSTREAM_URLS = {
    "tofu:1.10.6": "https://downloads.example/tofu-1.10.6",
    "tfsec:1.28.10": "https://downloads.example/tfsec-1.28.10",
    "infracost:0.10.39": "https://downloads.example/infracost-0.10.39",
}


def _snapshot_hashes() -> dict[str, str]:
    return {
        path.relative_to(_FIXTURE_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(_FIXTURE_ROOT.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
        and ".ruff_cache" not in path.parts
        and ".terraform" not in path.parts
    }


def _backend_fields(folder: str) -> dict[str, str]:
    versions = (_FIXTURE_ROOT / folder / "versions.tf").read_text()
    match = re.search(r'backend\s+"s3"\s*\{([^}]*)\}', versions, re.DOTALL)
    assert match is not None
    fields: dict[str, str] = {}
    for key, quoted, boolean in re.findall(
        r"^\s*(\w+)\s*=\s*(?:\"([^\"]*)\"|(true|false))",
        match.group(1),
        re.MULTILINE,
    ):
        fields[key] = quoted or boolean
    return fields


def test_sample_repo_fixture_is_exact_tracked_snapshot():
    snapshot = json.loads(_SNAPSHOT_PATH.read_text())
    assert set(snapshot["covered_folders"]) == set(_COVERED_FOLDERS)
    assert snapshot["files"] == _snapshot_hashes()


def test_sample_repo_fixture_discovers_all_folders():
    assert discover_folders(_FIXTURE_ROOT) == [
        "terraform/ap-northeast-1/01-vpc",
        "terraform/ap-northeast-1/02-ec2",
        "terraform/eu-west-1/01-vpc",
        "terraform/eu-west-1/02-ec2",
        "terraform/target-eu-west-1/01-vpc",
        "terraform/target-us-east-1/01-vpc",
    ]


def test_sample_repo_pipeline_fixture_resolves_ordered_steps():
    resolved = resolve_outer_state(
        str(_FIXTURE_ROOT),
        [],
        _UPSTREAM_URLS,
        "plan",
        pipeline="sample/eu-west-1",
    )
    assert resolved["folders"] == [
        "terraform/eu-west-1/01-vpc",
        "terraform/eu-west-1/02-ec2",
    ]
    assert resolved["steps"] == [
        ["terraform/eu-west-1/01-vpc"],
        ["terraform/eu-west-1/02-ec2"],
    ]


def test_sample_repo_folders_resolve_through_current_plan_contract():
    resolved = resolve_outer_state(
        str(_FIXTURE_ROOT),
        list(_COVERED_FOLDERS),
        _UPSTREAM_URLS,
        "plan",
    )
    assert resolved["upstream_urls"] == _UPSTREAM_URLS
    for folder, expected in _COVERED_FOLDERS.items():
        config = resolved["folder_configs"][folder]
        assert config["account_alias"] == expected["account_alias"]
        assert config["execution_target"] == "lambda"
        assert config["tf_runtime"] == "tofu:1.10.6"
        assert config["timeout"] == 900


def test_sample_repo_backends_match_scoped_lock_contract():
    for folder, expected in _COVERED_FOLDERS.items():
        fields = _backend_fields(folder)
        account_id = expected["account_id"]
        assert fields == {
            "bucket": f"openci-tf-state-{account_id}",
            "key": f"targets/<REPO_ORG>/<REPO_NAME>/{folder}.tfstate",
            "region": "us-east-1",
            "encrypt": "true",
        }
        terraform = "\n".join(
            path.read_text() for path in sorted((_FIXTURE_ROOT / folder).glob("*.tf"))
        )
        assert f'allowed_account = "{account_id}"' in terraform
        assert f'aws_region      = "{expected["region"]}"' in terraform
        assert 'resource "terraform_data" "account_guard"' in terraform
