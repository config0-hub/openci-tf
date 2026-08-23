# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static policy semantics tests for executor IAM deny statements."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path

import pytest  # type: ignore[import-not-found]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_READONLY_MODULE = _REPO_ROOT / "infra/modules/executor-readonly/main.tf"
_POWERUSER_MODULE = _REPO_ROOT / "infra/modules/executor-poweruser/main.tf"
_HUB_READONLY = _REPO_ROOT / "infra/modules/hub-setup/local_executor_readonly.tf"
_HUB_LEGACY_LOCAL = _REPO_ROOT / "infra/modules/hub-setup/local_executor.tf"
_LEGACY_REMOTE_MODULE = _REPO_ROOT / "infra/modules/target-connect/main.tf"
_HUB_SETUP_IAM_FILES = (_HUB_READONLY, _HUB_LEGACY_LOCAL)
_POLICY_MODULES = (_READONLY_MODULE, _HUB_READONLY)
_DENY_SID = "DenyInfrastructureMutationOutsideStateAndLock"
_ACTION_BLOCK_RE = re.compile(
    rf'Sid\s*=\s*"{_DENY_SID}"[\s\S]*?Action\s*=\s*(?:concat\(|\[)([\s\S]*?)NotResource',
    re.MULTILINE,
)
_SID_BLOCK_RE = re.compile(
    r'Sid\s*=\s*"([^"]+)"[\s\S]*?Effect\s*=\s*"Deny"[\s\S]*?Action\s*=\s*(?:concat\(|\[)([\s\S]*?)(?:NotResource|Resource\s*=)',
    re.MULTILINE,
)
_QUOTED_ACTION_RE = re.compile(r'"([^"]+)"')

_IAM_READ_ACTIONS = (
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListRolePolicies",
    "iam:ListAttachedRolePolicies",
    "iam:GetInstanceProfile",
    "iam:ListInstanceProfilesForRole",
    "iam:ListRoles",
    "iam:SimulatePrincipalPolicy",
)
_IAM_MUTATION_ACTIONS = (
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:PutRolePolicy",
    "iam:UpdateRole",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:AddRoleToInstanceProfile",
    "iam:RemoveRoleFromInstanceProfile",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:PassRole",
    "iam:CreateServiceLinkedRole",
    "iam:ChangePassword",
    "iam:UploadServerCertificate",
    "iam:ImportServerCertificate",
    "iam:SetDefaultPolicyVersion",
)
_STATE_BUCKET_NON_BACKEND_ACTIONS = (
    "s3:PutInventoryConfiguration",
    "s3:DeleteInventoryConfiguration",
    "s3:PutAnalyticsConfiguration",
    "s3:PutMetricsConfiguration",
    "s3:PutAccelerateConfiguration",
    "s3:PutBucketPolicy",
    "s3:DeleteBucket",
)
_LOCK_TABLE_CONTROL_ACTIONS = (
    "dynamodb:UpdateTable",
    "dynamodb:DeleteTable",
    "dynamodb:CreateTable",
)


def _extract_deny_actions(tf_text: str, sid: str = _DENY_SID) -> list[str]:
    if sid == _DENY_SID:
        match = _ACTION_BLOCK_RE.search(tf_text)
        assert match is not None, f"missing {_DENY_SID} statement"
        return _QUOTED_ACTION_RE.findall(match.group(1))
    for found_sid, action_block in _SID_BLOCK_RE.findall(tf_text):
        if found_sid == sid:
            return _QUOTED_ACTION_RE.findall(action_block)
    raise AssertionError(f"missing deny sid {sid}")


def _extract_sid_resource(tf_text: str, sid: str) -> str:
    pattern = rf'Sid\s*=\s*"{sid}"[\s\S]*?Resource\s*=\s*(\*|"[^"]+"|var\.[^\s]+)'
    match = re.search(pattern, tf_text)
    assert match, f"missing Resource for sid {sid}"
    return match.group(1)


def _action_denied(action: str, deny_patterns: list[str]) -> bool:
    return any(fnmatch(action, pattern) for pattern in deny_patterns)


@pytest.fixture(params=_POLICY_MODULES, ids=lambda path: path.parent.name)
def deny_actions(request: pytest.FixtureRequest) -> list[str]:
    path = request.param
    if not path.is_file():
        pytest.skip(f"missing policy module at {path}")
    actions = _extract_deny_actions(path.read_text())
    iam_actions = [action for action in actions if action.startswith("iam:")]
    assert iam_actions, "expected IAM deny actions in infrastructure mutation statement"
    assert "iam:*" not in iam_actions, (
        "broad iam:* deny blocks Terraform plan-time reads"
    )
    return actions


@pytest.mark.parametrize("read_action", _IAM_READ_ACTIONS)
def test_iam_plan_reads_are_not_explicitly_denied(
    deny_actions: list[str], read_action: str
):
    assert not _action_denied(read_action, deny_actions)


@pytest.mark.parametrize("mutation_action", _IAM_MUTATION_ACTIONS)
def test_iam_mutations_remain_explicitly_denied(
    deny_actions: list[str], mutation_action: str
):
    assert _action_denied(mutation_action, deny_actions)


@pytest.mark.parametrize(
    "module",
    (_READONLY_MODULE, _HUB_READONLY),
    ids=("executor-readonly", "hub-local-readonly"),
)
def test_readonly_roles_keep_non_iam_mutation_guards(module: Path):
    deny_actions = _extract_deny_actions(module.read_text())
    assert _action_denied("cloudformation:CreateStack", deny_actions)
    assert _action_denied("ec2:RunInstances", deny_actions)
    assert _action_denied("s3:CreateBucket", deny_actions)
    assert _action_denied("dynamodb:CreateTable", deny_actions)


def test_poweruser_role_uses_separate_unconditional_iam_deny():
    source = _POWERUSER_MODULE.read_text()
    actions = _extract_deny_actions(source, "DenyIamAndCloudFormationUnconditionally")
    assert not _action_denied("iam:PassRole", actions)
    assert not _action_denied("iam:DetachRolePolicy", actions)
    assert not _action_denied("iam:RemoveRoleFromInstanceProfile", actions)
    assert _action_denied("iam:ResetServiceSpecificCredential", actions)
    assert _action_denied("iam:CreateServiceLinkedRole", actions)
    assert _action_denied("cloudformation:CreateStack", actions)
    assert (
        _extract_sid_resource(source, "DenyIamAndCloudFormationUnconditionally")
        == '"*"'
    )


def test_poweruser_grants_scoped_workload_iam_lifecycle():
    source = _POWERUSER_MODULE.read_text()
    for sid in ("TerraformWorkloadIamRoleLifecycle",):
        block = source.split(f'Sid      = "{sid}"', 1)[1].split("},", 1)[0]
        assert "terraform_workload_iam_role_lifecycle_actions" in block
        assert "target_iam_role_arns" in block
        assert 'Resource = "*"' not in block
    for sid in ("TerraformWorkloadIamInstanceProfileLifecycle",):
        block = source.split(f'Sid      = "{sid}"', 1)[1].split("},", 1)[0]
        assert "terraform_workload_iam_instance_profile_lifecycle_actions" in block
        assert "target_iam_instance_profile_arns" in block
        assert 'Resource = "*"' not in block
    for sid in ("DenyIamLifecycleOutsideWorkloadResources",):
        block = source.split(f'Sid    = "{sid}"', 1)[1].split("},", 1)[0]
        assert "NotResource" in block
        assert "target_iam_role_arns" in block
        assert "target_iam_instance_profile_arns" in block


def test_poweruser_inline_grants_terraform_plan_time_iam_reads():
    source = _POWERUSER_MODULE.read_text()
    scoped_block = source.split('Sid      = "TerraformPlanTimeIamReadsScoped"', 1)[
        1
    ].split(
        'Sid      = "TerraformPlanTimeIamReadsWildcard"',
        1,
    )[0]
    wildcard_block = source.split('Sid      = "TerraformPlanTimeIamReadsWildcard"', 1)[
        1
    ].split(
        'Sid      = "DenyListBucketWithoutTargetPrefix"',
        1,
    )[0]
    assert "terraform_plan_time_iam_read_scoped_actions" in scoped_block
    assert "terraform_plan_time_iam_read_scoped_resources" in scoped_block
    assert "target_iam_role_arns" in source
    assert '"*"' not in scoped_block
    assert "terraform_plan_time_iam_read_wildcard_actions" in wildcard_block
    assert 'Resource = "*"' in wildcard_block


def test_poweruser_uses_inline_guards_without_a_permissions_boundary():
    source = _POWERUSER_MODULE.read_text()
    assert "permissions_boundary" not in source
    assert "executor_poweruser_permissions_boundary" not in source
    assert "DenyProtectedHubResources" in source


@pytest.mark.parametrize(
    "module",
    (_READONLY_MODULE, _HUB_READONLY),
    ids=("executor-readonly", "hub-local-readonly"),
)
def test_readonly_inline_grants_terraform_plan_time_iam_reads(module: Path):
    source = module.read_text()
    scoped_block = source.split('Sid      = "TerraformPlanTimeIamReadsScoped"', 1)[
        1
    ].split(
        'Sid      = "TerraformPlanTimeIamReadsWildcard"',
        1,
    )[0]
    wildcard_block = source.split('Sid      = "TerraformPlanTimeIamReadsWildcard"', 1)[
        1
    ].split(
        'Sid      = "DenyListBucketWithoutTargetPrefix"',
        1,
    )[0]
    assert "terraform_plan_time_iam_read_scoped_actions" in scoped_block
    assert "terraform_plan_time_iam_read_scoped_resources" in scoped_block
    assert '"*"' not in scoped_block
    assert "terraform_plan_time_iam_read_wildcard_actions" in wildcard_block
    assert 'Resource = "*"' in wildcard_block


@pytest.mark.parametrize(
    "module",
    (_READONLY_MODULE, _HUB_READONLY),
    ids=("executor-readonly", "hub-local-readonly"),
)
def test_readonly_boundary_grants_terraform_plan_time_iam_reads(module: Path):
    source = module.read_text()
    scoped_block = source.split(
        'Sid      = "BoundaryTerraformPlanTimeIamReadsScoped"', 1
    )[1].split(
        'Sid      = "BoundaryTerraformPlanTimeIamReadsWildcard"',
        1,
    )[0]
    wildcard_block = source.split(
        'Sid      = "BoundaryTerraformPlanTimeIamReadsWildcard"', 1
    )[1].split(
        '    ]\n  })\n}\n\nresource "aws_iam_role" "executor_readonly"',
        1,
    )[0]
    assert "terraform_plan_time_iam_read_scoped_actions" in scoped_block
    assert "terraform_plan_time_iam_read_scoped_resources" in scoped_block
    assert '"*"' not in scoped_block
    assert "terraform_plan_time_iam_read_wildcard_actions" in wildcard_block
    assert 'Resource = "*"' in wildcard_block


def test_legacy_executor_local_inline_grants_terraform_plan_time_iam_reads():
    source = _HUB_LEGACY_LOCAL.read_text()
    scoped_block = source.split('Sid      = "TerraformPlanTimeIamReadsScoped"', 1)[
        1
    ].split(
        'Sid      = "TerraformPlanTimeIamReadsWildcard"',
        1,
    )[0]
    wildcard_block = source.split('Sid      = "TerraformPlanTimeIamReadsWildcard"', 1)[
        1
    ].split(
        'Sid      = "DenyListBucketWithoutTargetPrefix"',
        1,
    )[0]
    assert "terraform_plan_time_iam_read_scoped_actions" in scoped_block
    assert "terraform_plan_time_iam_read_scoped_resources" in scoped_block
    assert '"*"' not in scoped_block
    assert "terraform_plan_time_iam_read_wildcard_actions" in wildcard_block
    assert 'Resource = "*"' in wildcard_block


def test_hub_setup_defines_shared_terraform_plan_time_iam_read_locals_once():
    source = "\n".join(path.read_text() for path in _HUB_SETUP_IAM_FILES)
    for local_name in (
        "hub_iam_role_arns",
        "hub_iam_instance_profile_arns",
        "terraform_plan_time_iam_read_scoped_actions",
        "terraform_plan_time_iam_read_scoped_resources",
        "terraform_plan_time_iam_read_wildcard_actions",
    ):
        assert len(re.findall(rf"^\s*{local_name}\s*=", source, re.MULTILINE)) == 1


def test_legacy_remote_inline_grants_terraform_plan_time_iam_reads():
    source = _LEGACY_REMOTE_MODULE.read_text()
    scoped_block = source.split('Sid      = "TerraformPlanTimeIamReadsScoped"', 1)[
        1
    ].split(
        'Sid      = "TerraformPlanTimeIamReadsWildcard"',
        1,
    )[0]
    wildcard_block = source.split('Sid      = "TerraformPlanTimeIamReadsWildcard"', 1)[
        1
    ].split(
        'Sid      = "DenyListBucketWithoutTargetPrefix"',
        1,
    )[0]
    assert "terraform_plan_time_iam_read_scoped_actions" in scoped_block
    assert "terraform_plan_time_iam_read_scoped_resources" in scoped_block
    assert "target_iam_role_arns" in source
    assert '"*"' not in scoped_block
    assert "terraform_plan_time_iam_read_wildcard_actions" in wildcard_block
    assert 'Resource = "*"' in wildcard_block


@pytest.mark.parametrize("read_action", _IAM_READ_ACTIONS)
def test_legacy_remote_iam_plan_reads_are_not_explicitly_denied(read_action: str):
    deny_actions = _extract_deny_actions(_LEGACY_REMOTE_MODULE.read_text())
    assert not _action_denied(read_action, deny_actions)


@pytest.mark.parametrize("mutation_action", _IAM_MUTATION_ACTIONS)
def test_legacy_remote_iam_mutations_remain_explicitly_denied(mutation_action: str):
    deny_actions = _extract_deny_actions(_LEGACY_REMOTE_MODULE.read_text())
    assert _action_denied(mutation_action, deny_actions)


@pytest.mark.parametrize("control_action", _STATE_BUCKET_NON_BACKEND_ACTIONS)
def test_readonly_denies_state_bucket_non_backend_primitives(control_action: str):
    for module in (_READONLY_MODULE, _HUB_READONLY):
        source = module.read_text()
        block = source.split("DenyStateBucketNonBackendPrimitives", 1)[1].split(
            "DenyInfrastructureMutationOutsideStateAndLock", 1
        )[0]
        assert "NotAction" in block
        assert '"s3:GetObject"' in block
        assert '"s3:ListBucket"' in block
        assert control_action not in block.split("NotAction", 1)[1]


@pytest.mark.parametrize("control_action", _STATE_BUCKET_NON_BACKEND_ACTIONS)
def test_poweruser_denies_state_bucket_non_backend_primitives(control_action: str):
    source = _POWERUSER_MODULE.read_text()
    block = source.split("DenyStateBucketNonBackendPrimitives", 1)[1].split(
        "DenyIamAndCloudFormationUnconditionally", 1
    )[0]
    assert "NotAction" in block
    assert '"s3:GetObject"' in block
    assert '"s3:ListBucket"' in block
    assert control_action not in block.split("NotAction", 1)[1]


def test_poweruser_role_uses_notaction_lock_guard():
    source = _POWERUSER_MODULE.read_text()
    assert "DenyLockTableNonBackendPrimitives" in source
    assert (
        "NotAction"
        in source.split("DenyLockTableNonBackendPrimitives", 1)[1].split(
            "DenyLockItemsOutsideTargets", 1
        )[0]
    )
    assert "DenyLockTableControlPlane" not in source


@pytest.mark.parametrize("control_action", _LOCK_TABLE_CONTROL_ACTIONS)
def test_poweruser_denies_lock_table_control_plane_via_notaction(control_action: str):
    source = _POWERUSER_MODULE.read_text()
    block = source.split("DenyLockTableNonBackendPrimitives", 1)[1].split(
        "DenyLockItemsOutsideTargets", 1
    )[0]
    assert "NotAction" in block
    assert '"dynamodb:DescribeTable"' in block
    assert control_action not in block.split("NotAction", 1)[1]


def test_poweruser_preserves_target_state_data_path_allowance():
    source = _POWERUSER_MODULE.read_text()
    assert 'Sid      = "TerraformTargetStateReadWrite"' in source
    assert "${var.state_bucket_arn}/targets/*" in source
    assert '"dynamodb:UpdateItem"' in source


def test_poweruser_denies_iam_reset_mutations():
    actions = _extract_deny_actions(
        _POWERUSER_MODULE.read_text(), "DenyIamAndCloudFormationUnconditionally"
    )
    assert _action_denied("iam:ResetServiceSpecificCredential", actions)


@pytest.mark.parametrize(
    "module", (*_POLICY_MODULES, _POWERUSER_MODULE), ids=lambda path: path.parent.name
)
def test_executor_lock_access_is_scoped_to_target_state_keys(module: Path):
    source = module.read_text()
    assert 'Sid       = "TerraformTargetLockReadWrite"' in source
    for action in ("GetItem", "PutItem", "DeleteItem", "UpdateItem", "DescribeTable"):
        assert f'"dynamodb:{action}"' in source
    assert '"dynamodb:LeadingKeys" = ["*/targets/*"]' in source
    if module == _POWERUSER_MODULE:
        assert "DenyLockTableNonBackendPrimitives" in source
    else:
        assert 'Sid      = "DenyLockTableBroadReads"' in source
    assert 'Sid       = "DenyLockItemsOutsideTargets"' in source
    assert "var.lock_table_arn" in source or "lock_table_arn" in source


def test_target_connect_wires_account_local_lock_table_arn():
    source = (_REPO_ROOT / "infra/target-connect/main.tf").read_text()
    assert (
        'lock_table_arn           = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.project_name}-tf-locks"'
        in source
    )
    assert "lock_table_arn           = local.lock_table_arn" in source
