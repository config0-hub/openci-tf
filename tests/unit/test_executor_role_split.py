# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static tests for split executor-readonly and executor-poweruser roles."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_READONLY_MODULE = _REPO_ROOT / "infra/modules/executor-readonly/main.tf"
_POWERUSER_MODULE = _REPO_ROOT / "infra/modules/executor-poweruser/main.tf"
_READONLY_ROOT = _REPO_ROOT / "infra/target-connect/main.tf"
_POWERUSER_ROOT = _REPO_ROOT / "infra/target-connect-poweruser/main.tf"
_HUB_READONLY = _REPO_ROOT / "infra/modules/hub-setup/local_executor_readonly.tf"
_JUSTFILE = _REPO_ROOT / "justfile"
_EXECUTOR_ROLES_DOC = _REPO_ROOT / "docs/EXECUTOR_ROLES.md"


def test_readonly_and_poweruser_use_distinct_role_resource_names():
    readonly = _READONLY_MODULE.read_text(encoding="utf-8")
    poweruser = _POWERUSER_MODULE.read_text(encoding="utf-8")
    assert 'resource "aws_iam_role" "executor_readonly"' in readonly
    assert 'resource "aws_iam_role" "executor_poweruser"' in poweruser
    assert "executor_poweruser" not in readonly
    assert "executor_readonly" not in poweruser


def test_readonly_never_attaches_power_user_access():
    readonly = _READONLY_MODULE.read_text(encoding="utf-8")
    assert "executor_readonly_read_only" in readonly
    assert 'policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"' in readonly
    assert "executor_poweruser" not in readonly
    assert 'policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"' not in readonly


def test_poweruser_never_attaches_read_only_access():
    poweruser = _POWERUSER_MODULE.read_text(encoding="utf-8")
    assert "executor_poweruser_power_user" in poweruser
    assert 'policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"' in poweruser
    assert "executor_readonly" not in poweruser
    assert 'policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"' not in poweruser


def test_readonly_role_has_permissions_boundary():
    readonly = _READONLY_MODULE.read_text(encoding="utf-8")
    assert "executor_readonly_permissions_boundary" in readonly
    assert (
        "permissions_boundary = aws_iam_policy.executor_readonly_permissions_boundary.arn"
        in readonly
    )
    hub_readonly = _HUB_READONLY.read_text(encoding="utf-8")
    assert "executor_readonly_permissions_boundary" in hub_readonly
    assert (
        "permissions_boundary = aws_iam_policy.executor_readonly_permissions_boundary.arn"
        in hub_readonly
    )


def test_poweruser_trusts_only_mutation_prepare_roles_and_hub_control_role():
    poweruser = _POWERUSER_MODULE.read_text(encoding="utf-8")
    trust_block = poweruser.split("hub_role_prefix_arns = [", 1)[1].split("]", 1)[0]
    assert "-run-folder-apply-prepare-and-submit" in trust_block
    assert "-run-folder-destroy-prepare-and-submit" in trust_block
    assert "-hub-lambda-exec" in trust_block
    assert 'role/${var.role_prefix}-run-folder-prepare-and-submit"' not in trust_block


def test_poweruser_role_uses_explicit_denies_without_permissions_boundary():
    poweruser = _POWERUSER_MODULE.read_text(encoding="utf-8")
    assert "executor_poweruser_permissions_boundary" not in poweruser
    assert "permissions_boundary" not in poweruser
    assert "DenyProtectedHubResources" in poweruser
    assert "DenyStateObjectOutsideTargets" not in poweruser
    assert "aws:ResourceArn" not in poweruser


def test_poweruser_denies_iam_cloudformation_with_unconditional_resource_star():
    poweruser = _POWERUSER_MODULE.read_text(encoding="utf-8")
    assert "DenyIamAndCloudFormationUnconditionally" in poweruser
    assert "DenyStateBucketNonBackendPrimitives" in poweruser
    assert "DenyLockTableNonBackendPrimitives" in poweruser
    assert "DenyInfrastructureMutationOutsideStateAndLock" not in poweruser
    block = poweruser.split("DenyIamAndCloudFormationUnconditionally", 1)[1].split(
        "},", 1
    )[0]
    assert 'Resource = "*"' in block
    assert "NotResource" not in block


def test_separate_target_roots_have_independent_module_boundaries():
    readonly_root = _READONLY_ROOT.read_text(encoding="utf-8")
    poweruser_root = _POWERUSER_ROOT.read_text(encoding="utf-8")
    assert 'module "executor_readonly"' in readonly_root
    assert "executor_poweruser" not in readonly_root
    assert 'module "executor_poweruser"' in poweruser_root
    assert "executor_readonly" not in poweruser_root


def test_target_connect_refuses_same_account_via_precondition():
    readonly_root = _READONLY_ROOT.read_text(encoding="utf-8")
    assert 'resource "terraform_data" "remote_account_only"' in readonly_root
    assert "local.hub_account_id != local.account_id" in readonly_root
    assert "moved {" not in readonly_root


def test_hub_readonly_preserves_legacy_executor_local():
    hub_legacy = (_REPO_ROOT / "infra/modules/hub-setup/local_executor.tf").read_text(
        encoding="utf-8"
    )
    hub_readonly = _HUB_READONLY.read_text(encoding="utf-8")
    assert "moved {" not in hub_legacy
    assert "moved {" not in hub_readonly
    assert 'resource "aws_iam_role" "executor_local"' in hub_legacy
    assert 'resource "aws_iam_role" "executor_readonly"' in hub_readonly


def test_target_connect_preserves_legacy_executor_remote():
    readonly_root = _READONLY_ROOT.read_text(encoding="utf-8")
    assert 'module "target_connect"' in readonly_root
    assert 'module "executor_readonly"' in readonly_root
    assert "moved {" not in readonly_root


def test_target_aws_role_readonly_destroy_uses_targeted_module_only():
    script = (_REPO_ROOT / "scripts/target_aws_role.sh").read_text(encoding="utf-8")
    destroy_section = script.split('if [ "$ACTION" = "create" ]; then', 1)[1]
    assert "-target=module.executor_readonly" in destroy_section
    assert (
        'terraform -chdir="$TF_ROOT" destroy -input=false -auto-approve'
        in destroy_section
    )


def test_executor_roles_doc_describes_current_role_ownership():
    text = _EXECUTOR_ROLES_DOC.read_text(encoding="utf-8")
    assert "openci-tf-executor-readonly" in text
    assert "openci-tf-executor-poweruser" in text
    assert "hub-setup" in text
    assert "target-create-aws-readonly" in text
    assert "target-create-aws-poweruser" in text


def test_hub_setup_declares_provision_legacy_variable():
    variables = (_REPO_ROOT / "infra/modules/hub-setup/variables.tf").read_text(
        encoding="utf-8"
    )
    assert "provision_legacy_executor_local" in variables
    assert (
        "default     = true" in variables.split("provision_legacy_executor_local", 1)[1]
    )


def test_target_connect_declares_provision_legacy_variable():
    variables = (_REPO_ROOT / "infra/modules/target-connect/variables.tf").read_text(
        encoding="utf-8"
    )
    assert "provision_legacy_executor_remote" in variables


def test_legacy_remote_policy_denies_new_split_state_roots():
    source = (_REPO_ROOT / "infra/modules/target-connect/main.tf").read_text(
        encoding="utf-8"
    )
    assert "target-connect-readonly/*" in source
    assert "target-connect-poweruser/*" in source
    assert "DenyListBucketOutsideTargetsPrefix" in source
    assert "DenyStateBucketNonBackendPrimitives" in source


def test_legacy_remote_attachment_respects_enable_apply():
    source = (_REPO_ROOT / "infra/modules/target-connect/main.tf").read_text(
        encoding="utf-8"
    )
    assert "provision_legacy_executor_remote && !var.enable_apply" in source
    assert "provision_legacy_executor_remote && var.enable_apply" in source


def test_target_connect_root_passes_enable_apply_to_legacy_module():
    source = (_REPO_ROOT / "infra/target-connect/main.tf").read_text(encoding="utf-8")
    assert "enable_apply                     = var.enable_apply" in source


def test_deploy_and_target_recipes_read_provision_legacy_from_ssm():
    justfile = _JUSTFILE.read_text(encoding="utf-8")
    deploy_section = justfile.split("deploy:", 1)[1].split("deploy-destroy:", 1)[0]
    assert "provision_legacy_executor_local" in deploy_section
    assert "enable_apply" in deploy_section
    target_script = (_REPO_ROOT / "scripts/target_aws_role.sh").read_text(
        encoding="utf-8"
    )
    assert "provision_legacy_executor_remote" in target_script
    assert "enable_apply" in target_script
    assert 'TFVARS+=("enable_apply=${ENABLE_APPLY}")' in target_script
    retire_script = (_REPO_ROOT / "scripts/retire_legacy_executor.sh").read_text(
        encoding="utf-8"
    )
    assert "provision_legacy_executor_local" in retire_script
    assert "provision_legacy_executor_remote" in retire_script


def test_justfile_lists_legacy_retirement_recipes():
    text = _JUSTFILE.read_text(encoding="utf-8")
    for recipe in (
        "retire-legacy-executor-local",
        "restore-legacy-executor-local",
        "retire-legacy-executor-remote",
        "restore-legacy-executor-remote",
    ):
        assert recipe in text


def test_executor_roles_doc_describes_lane_binding_and_state_contract():
    text = _EXECUTOR_ROLES_DOC.read_text(encoding="utf-8")
    assert "role_name" in text
    assert "poweruser_role_name" in text
    assert "enable_apply" in text
    assert "targets/<repo>/<folder>.tfstate" in text
    assert "source snapshots" in text


def test_justfile_install_does_not_invoke_target_create_on_hub():
    text = _JUSTFILE.read_text(encoding="utf-8")
    install = text.split("install:", 1)[1].split("# --- journeys", 1)[0]
    assert "target-create-aws-readonly" not in install


def test_justfile_uninstall_probes_poweruser_footprint_not_role_only():
    text = _JUSTFILE.read_text(encoding="utf-8")
    uninstall = text.split("uninstall:", 1)[1].split("verify:", 1)[0]
    assert "poweruser_needs_destroy.sh" in uninstall
    assert "./scripts/role_probe.sh" not in uninstall


def test_justfile_lists_split_target_recipes():
    text = _JUSTFILE.read_text(encoding="utf-8")
    for recipe in (
        "target-create-aws-readonly",
        "target-delete-aws-readonly",
        "target-create-aws-poweruser",
        "target-delete-aws-poweruser",
    ):
        assert recipe in text


def test_target_recipes_forward_positional_arguments_safely():
    text = _JUSTFILE.read_text(encoding="utf-8")
    for recipe in (
        "target-create-aws-readonly",
        "target-delete-aws-readonly",
        "target-create-aws-poweruser",
        "target-delete-aws-poweruser",
    ):
        section = text.split(f"{recipe} ", 1)[1].split("\n\n", 1)[0]
        assert "{{hub_account_id}}" not in section
        assert "{{state_bucket}}" not in section
        assert 'hub_account_id="${1' in section


def test_target_aws_role_script_refuses_same_account_readonly(tmp_path):
    script = _REPO_ROOT / "scripts/target_aws_role.sh"
    aws = tmp_path / "aws"
    aws.write_text(
        """#!/usr/bin/env bash
case "$1" in
sts)
  echo "123456789012"
  exit 0
  ;;
*)
  exit 99
  ;;
esac
"""
    )
    aws.chmod(0o755)
    result = subprocess.run(
        [
            str(script),
            "--action",
            "create",
            "--role",
            "readonly",
            "--hub-account-id",
            "123456789012",
        ],
        cwd=_REPO_ROOT,
        env={"PATH": f"{tmp_path}:{__import__('os').environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "refuses same-account" in result.stderr


def test_target_aws_role_script_allows_same_account_poweruser(tmp_path):
    script = _REPO_ROOT / "scripts/target_aws_role.sh"
    aws = tmp_path / "aws"
    aws.write_text(
        """#!/usr/bin/env bash
case "$1" in
sts)
  echo "123456789012"
  exit 0
  ;;
*)
  exit 99
  ;;
esac
"""
    )
    aws.chmod(0o755)
    result = subprocess.run(
        [
            str(script),
            "--action",
            "create",
            "--role",
            "poweruser",
            "--hub-account-id",
            "123456789012",
        ],
        cwd=_REPO_ROOT,
        env={"PATH": f"{tmp_path}:{__import__('os').environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 1 or "refuses same-account" not in result.stderr


def test_target_aws_role_script_has_no_shell_interpolation_of_user_input():
    script = (_REPO_ROOT / "scripts/target_aws_role.sh").read_text(encoding="utf-8")
    assert "./scripts/target_aws_role.sh" in (_REPO_ROOT / "justfile").read_text(
        encoding="utf-8"
    )
    assert "eval " not in script
    assert "${!" not in script
    assert "target_connect_state_bucket.sh" not in script


def test_hub_setup_has_single_caller_identity_data_source():
    main = (_REPO_ROOT / "infra/modules/hub-setup/main.tf").read_text(encoding="utf-8")
    local = _HUB_READONLY.read_text(encoding="utf-8")
    assert len(re.findall(r'data "aws_caller_identity" "current"', main + local)) == 1
