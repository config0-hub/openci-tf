"""Target-connect installer state is S3-only; its executor gets scoped target locks."""
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


def test_target_connect_executor_has_scoped_lock_table_iam():
    module = (_READONLY_MODULE / "main.tf").read_text()
    variables = (_READONLY_MODULE / "variables.tf").read_text()
    root = (_TARGET_CONNECT_ROOT / "main.tf").read_text()
    assert "lock_table_arn" in variables
    assert "lock_table_arn           = local.lock_table_arn" in root
    assert "TerraformTargetLockReadWrite" in module
    assert '"dynamodb:LeadingKeys" = ["*/targets/*"]' in module
    assert "DenyLockTableBroadReads" in module
    assert "DenyLockItemsOutsideTargets" in module


def test_hub_local_executor_retains_lock_table_iam():
    text = _HUB_LOCAL_EXECUTOR.read_text()
    assert "lock_table_arn" in text
    assert "TerraformTargetLockReadWrite" in text
    assert "DenyLockTableBroadReads" in text


def test_bootstrap_still_provisions_lock_table():
    if not _BOOTSTRAP_MAIN.is_file():
        pytest.skip("bootstrap main.tf is not available in this test environment")
    text = _BOOTSTRAP_MAIN.read_text()
    assert 'resource "aws_dynamodb_table" "locks"' in text


def test_target_connect_installer_backend_includes_lock_table(tmp_path: Path):
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
            "openci-tf-tf-locks",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    backend = (target_dir / "backend.tf").read_text()
    assert 'dynamodb_table = "openci-tf-tf-locks"' in backend
    assert 'bucket = "openci-tf-state-123456789012"' in backend
    assert 'key    = "target-connect/terraform.tfstate"' in backend


def test_poweruser_role_omits_readonly_era_infrastructure_denies():
    source = (_REPO_ROOT / "infra/modules/executor-poweruser/main.tf").read_text()
    assert "DenyInfrastructureMutationOutsideStateAndLock" not in source
    assert "DenyIamAndCloudFormationUnconditionally" in source
    iam_block = source.split("DenyIamAndCloudFormationUnconditionally", 1)[1].split("},", 1)[0]
    assert '"ec2:Delete*"' not in iam_block
    assert '"cloudformation:*"' in iam_block
    assert 'Resource = "*"' in iam_block
