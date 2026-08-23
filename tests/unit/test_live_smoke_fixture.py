# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the repository snapshot used by the live API smoke runs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from src.domain.config.outer_state import discover_folders, resolve_outer_state

_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures/live-smoke/sample-target-repo"
)
_PROVENANCE_PATH = _FIXTURE_ROOT.with_suffix(".snapshot.json")
_SOURCE_REPOSITORY = "https://github.com/<REPO_ORG>/<REPO_NAME>"
_SOURCE_COMMIT = "2f772fca6dfab7c92c2444e77ce9efc08118c32d"
_LIVE_FOLDERS = {
    "terraform/ap-northeast-1/01-vpc": {
        "account_alias": "REPLACE_MAIN_ALIAS",
        "account_id": "REPLACE_MAIN_ACCOUNT",
        "region": "ap-northeast-1",
    },
    "terraform/test2-us-east-1/01-vpc": {
        "account_alias": "REPLACE_SECONDARY_ALIAS",
        "account_id": "REPLACE_SECONDARY_ACCOUNT",
        "region": "us-east-1",
    },
}
_UPSTREAM_URLS = {
    "tofu:1.8.0": "https://downloads.example/tofu-1.8.0",
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


def test_live_smoke_fixture_is_exact_provenanced_snapshot():
    provenance = json.loads(_PROVENANCE_PATH.read_text())
    assert provenance["source_repository"] == _SOURCE_REPOSITORY
    assert provenance["source_commit"] == _SOURCE_COMMIT
    assert set(provenance["live_tested_folders"]) == set(_LIVE_FOLDERS)
    assert provenance["files"] == _snapshot_hashes()


def test_live_smoke_fixture_discovers_all_original_folders():
    assert discover_folders(_FIXTURE_ROOT) == [
        "terraform/ap-northeast-1/01-vpc",
        "terraform/ap-northeast-1/02-ec2",
        "terraform/eu-west-1/01-vpc",
        "terraform/eu-west-1/02-ec2",
        "terraform/test2-eu-west-1/01-vpc",
        "terraform/test2-us-east-1/01-vpc",
    ]


def test_live_smoke_pipeline_fixture_resolves_ordered_steps():
    resolved = resolve_outer_state(
        str(_FIXTURE_ROOT),
        [],
        _UPSTREAM_URLS,
        "plan",
        pipeline="smoke/eu-west-1",
    )
    assert resolved["folders"] == [
        "terraform/eu-west-1/01-vpc",
        "terraform/eu-west-1/02-ec2",
    ]
    assert resolved["steps"] == [
        ["terraform/eu-west-1/01-vpc"],
        ["terraform/eu-west-1/02-ec2"],
    ]


def test_live_smoke_folders_resolve_through_current_plan_contract():
    resolved = resolve_outer_state(
        str(_FIXTURE_ROOT),
        list(_LIVE_FOLDERS),
        _UPSTREAM_URLS,
        "plan",
    )
    assert resolved["upstream_urls"] == _UPSTREAM_URLS
    for folder, expected in _LIVE_FOLDERS.items():
        config = resolved["folder_configs"][folder]
        assert config["account_alias"] == expected["account_alias"]
        assert config["execution_target"] == "lambda"
        assert config["tf_runtime"] == "tofu:1.8.0"
        assert config["timeout"] == 900


def test_live_smoke_backends_match_scoped_lock_contract():
    for folder, expected in _LIVE_FOLDERS.items():
        fields = _backend_fields(folder)
        account_id = expected["account_id"]
        assert fields == {
            "bucket": f"openci-tf-state-{account_id}",
            "key": f"targets/<REPO_ORG>/<REPO_NAME>/{folder}.tfstate",
            "region": "us-east-1",
            "dynamodb_table": "openci-tf-tf-locks",
            "encrypt": "true",
        }
        terraform = "\n".join(
            path.read_text() for path in sorted((_FIXTURE_ROOT / folder).glob("*.tf"))
        )
        assert f'allowed_account = "{account_id}"' in terraform
        assert f'aws_region      = "{expected["region"]}"' in terraform
        assert 'resource "terraform_data" "account_guard"' in terraform
