# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision 27: state locking is the S3 native lock file; no DynamoDB lock IAM,
provisioning, or lock-table backend plumbing remains. Legacy table references
exist only in recovery checks that safely remove pre-migration state."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TARGET_CONNECT_ROOT = _REPO_ROOT / "infra/target-connect"
_READONLY_MODULE = _REPO_ROOT / "infra/modules/executor-readonly"
_HUB_LOCAL_EXECUTOR = _REPO_ROOT / "infra/modules/hub-setup/local_executor_readonly.tf"
_BOOTSTRAP_MAIN = _REPO_ROOT / "infra/bootstrap/main.tf"
_GENERATE_BACKEND = _REPO_ROOT / "scripts/generate_backend.sh"
_INSTALL_DOC = _REPO_ROOT / "docs/INSTALL.md"
_VERIFY = _REPO_ROOT / "scripts/verify.sh"
_STATE_IDENTITY = _REPO_ROOT / "scripts/state_identity.sh"

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


def test_update_migration_runs_the_real_bootstrap_owner_before_verification():
    update = _INSTALL_DOC.read_text().split("## Updating an existing install", 1)[1].split(
        "## Enable apply and destroy", 1
    )[0]
    commands = ["just bootstrap", "just foundation", "just deploy", "just verify"]
    positions = [update.index(command) for command in commands]
    assert positions == sorted(positions)
    assert "Terraform >= 1.10" in update
    assert "removes the legacy `<project>-tf-locks` DynamoDB table" in update

    # The documented producer is the bootstrap root that formerly owned the
    # resource; its real post-migration configuration omits the table, while
    # the real verification consumer requires the physical table to be gone.
    assert 'resource "aws_dynamodb_table"' not in _BOOTSTRAP_MAIN.read_text()
    verify = _VERIFY.read_text()
    assert (
        'check "lock table ${PROJECT}-tf-locks (must never exist)" '
        '0 table_exists "${PROJECT}-tf-locks"'
    ) in verify


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
    backend_generation = [line for line in justfile.splitlines() if "generate_backend.sh" in line]
    assert backend_generation
    assert all("tf-locks" not in line for line in backend_generation)
    assert all("LEGACY_LOCK_TABLE" not in line for line in backend_generation)


def test_legacy_state_identity_accepts_only_the_former_physical_table_name(tmp_path: Path):
    bucket = "openci-tf-state-123456789012"
    expected_table = "openci-tf-tf-locks"

    def run(table_name: str) -> subprocess.CompletedProcess[str]:
        state = tmp_path / f"{table_name}.tfstate"
        state.write_text(
            json.dumps(
                {
                    "version": 4,
                    "resources": [
                        {
                            "mode": "managed",
                            "type": "aws_s3_bucket",
                            "name": "state",
                            "instances": [{"attributes": {"bucket": bucket}}],
                        },
                        {
                            "mode": "managed",
                            "type": "aws_dynamodb_table",
                            "name": "locks",
                            "instances": [{"attributes": {"name": table_name}}],
                        },
                    ],
                }
            )
        )
        return subprocess.run(
            [str(_STATE_IDENTITY), str(state), bucket, expected_table],
            capture_output=True,
            text=True,
            check=False,
        )

    assert run(expected_table).returncode == 0
    foreign = run("someone-elses-table")
    assert foreign.returncode == 1
    assert "refusing to use it" in foreign.stderr


def test_legacy_table_recovery_checks_live_ownership_before_terraform():
    justfile = (_REPO_ROOT / "justfile").read_text()
    bootstrap = justfile.split("bootstrap:", 1)[1].split("# Destroys the state bucket", 1)[0]
    destroy = justfile.split("bootstrap-destroy:", 1)[1].split("# --- component recipes", 1)[0]
    for recipe in (bootstrap, destroy):
        state_identity = recipe.index("./scripts/state_identity.sh")
        owner_probe = recipe.index("aws dynamodb list-tags-of-resource")
        terraform = recipe.index("terraform -chdir=infra/bootstrap")
        assert state_identity < owner_probe < terraform
        assert 'TABLE_OWNER" = "openci-tf-bootstrap"' in recipe


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
