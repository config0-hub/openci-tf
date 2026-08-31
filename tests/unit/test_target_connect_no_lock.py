# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision 27: state locking is the S3 native lock file; no DynamoDB lock IAM,
lock table, or lock-table backend plumbing exists anywhere."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET_CONNECT_ROOT = _REPO_ROOT / "infra/target-connect"
_READONLY_MODULE = _REPO_ROOT / "infra/modules/executor-readonly"
_HUB_LOCAL_EXECUTOR = _REPO_ROOT / "infra/modules/hub-setup/local_executor_readonly.tf"
_BOOTSTRAP_MAIN = _REPO_ROOT / "infra/bootstrap/main.tf"
_GENERATE_BACKEND = _REPO_ROOT / "scripts/generate_backend.sh"

_LOCK_FREE_TF_SOURCES = (
    "infra/bootstrap/main.tf",
    "infra/bootstrap/outputs.tf",
    "infra/deploy/data.tf",
    "infra/deploy/main.tf",
    "infra/modules/executor-readonly/main.tf",
    "infra/modules/executor-readonly/variables.tf",
    "infra/modules/executor-poweruser/main.tf",
    "infra/modules/executor-poweruser/variables.tf",
    "infra/modules/target-connect/main.tf",
    "infra/modules/target-connect/variables.tf",
    "infra/modules/hub-setup/main.tf",
    "infra/modules/hub-setup/variables.tf",
    "infra/modules/hub-setup/local_executor.tf",
    "infra/modules/hub-setup/local_executor_readonly.tf",
    "infra/target-connect/main.tf",
    "infra/target-connect-poweruser/main.tf",
)


@pytest.mark.parametrize("relative", _LOCK_FREE_TF_SOURCES)
def test_terraform_sources_carry_no_lock_table_plumbing(relative: str):
    path = _REPO_ROOT / relative
    if not path.is_file():
        pytest.skip(f"{relative} is not available in this test environment")
    text = path.read_text()
    assert "lock_table" not in text, relative
    assert "tf-locks" not in text, relative
    assert "dynamodb:LeadingKeys" not in text, relative


def test_bootstrap_provisions_no_lock_table():
    if not _BOOTSTRAP_MAIN.is_file():
        pytest.skip("bootstrap main.tf is not available in this test environment")
    text = _BOOTSTRAP_MAIN.read_text()
    assert 'resource "aws_dynamodb_table"' not in text


def test_bootstrap_recipe_clears_stale_foreign_backend_cache():
    justfile = (_REPO_ROOT / "justfile").read_text()
    bootstrap = justfile.split("bootstrap:", 1)[1].split("bootstrap-destroy:", 1)[0]
    assert bootstrap.count("./scripts/clear_stale_bootstrap_backend_cache.sh") >= 3
    assert "init -reconfigure -input=false" in bootstrap
    script = (_REPO_ROOT / "scripts/clear_stale_bootstrap_backend_cache.sh").read_text()
    assert "jq -er '.backend.config.bucket'" in script
    assert "infra/bootstrap/.terraform" in script
    assert "rm -rf infra/bootstrap/.terraform" in script


def test_justfile_backend_inits_pass_use_lockfile():
    justfile = (_REPO_ROOT / "justfile").read_text()
    assert "-backend-config=use_lockfile=true" in justfile
    assert "tf-locks" not in justfile
    assert "dynamodb" not in justfile.lower().replace("no dynamodb lock table exists", "")


def test_generated_backend_is_bucket_key_region_only(tmp_path: Path):
    if not _GENERATE_BACKEND.is_file():
        pytest.skip("generate_backend.sh is not available")
    target_dir = tmp_path / "target-connect"
    target_dir.mkdir()
    subprocess.run(
        [
            str(_GENERATE_BACKEND),
            "openci-tf-state-123456789012",
            "target-connect",
            "us-east-1",
            str(target_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    backend = (target_dir / "backend.tf").read_text()
    assert 'bucket = "openci-tf-state-123456789012"' in backend
    assert 'key    = "target-connect/terraform.tfstate"' in backend
    assert 'region = "us-east-1"' in backend
    assert "dynamodb_table" not in backend
    assert "use_lockfile" not in backend


def test_generate_backend_rejects_removed_lock_table_argument(tmp_path: Path):
    if not _GENERATE_BACKEND.is_file():
        pytest.skip("generate_backend.sh is not available")
    target_dir = tmp_path / "target-connect"
    target_dir.mkdir()
    result = subprocess.run(
        [
            str(_GENERATE_BACKEND),
            "openci-tf-state-123456789012",
            "target-connect",
            "us-east-1",
            str(target_dir),
            "openci-tf-tf-locks",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "lock-table argument was removed" in result.stderr


def test_poweruser_role_omits_readonly_era_infrastructure_denies():
    source = (_REPO_ROOT / "infra/modules/executor-poweruser/main.tf").read_text()
    assert "DenyInfrastructureMutationOutsideStateAndLock" not in source
    assert "DenyIamAndCloudFormationUnconditionally" in source
    iam_block = source.split("DenyIamAndCloudFormationUnconditionally", 1)[1].split("},", 1)[0]
    assert '"ec2:Delete*"' not in iam_block
    assert '"cloudformation:*"' in iam_block
    assert 'Resource = "*"' in iam_block
