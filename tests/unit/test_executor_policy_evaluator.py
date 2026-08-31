# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic evaluation tests for rendered executor-poweruser IAM policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.iam_policy_evaluator import (
    _STATE_BUCKET,
    evaluate_effective_policy,
    evaluate_inline_policy,
    evaluate_legacy_remote_poweruser_effective_policy,
    is_explicitly_denied,
    policy_without_statement_sid,
    poweruser_managed_policy_allows,
    render_legacy_remote_inline_policy,
    render_poweruser_boundary_policy,
    render_poweruser_inline_policy,
)

_TARGET_KEY = f"{_STATE_BUCKET}/targets/org/repo/folder.tfstate"
_NON_TARGET_KEY = f"{_STATE_BUCKET}/deploy/terraform.tfstate"
_LIST_PREFIX = {"s3:prefix": ["targets/foo"]}
_UNLISTED_CONTROL_KEY = f"{_STATE_BUCKET}/unlisted-control/terraform.tfstate"
_SOURCE_KEY = f"{_STATE_BUCKET}/source/manifest.json"
_DEPLOY_LIST_PREFIX = {"s3:prefix": ["deploy/foo"]}
_BROAD_LIST: dict[str, list[str]] = {}
_ACCOUNT_ID = "222222222222"
_FOREIGN_ROLE_ARN = "arn:aws:iam::999999999999:role/example"
_FOREIGN_INSTANCE_PROFILE_ARN = "arn:aws:iam::999999999999:instance-profile/example"
_UNRELATED_USER_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:user/example"
_UNRELATED_GROUP_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:group/example"
_HUB_ACCOUNT_ID = "111111111111"
_PROTECTED_HUB_CASES = [
    ("dynamodb:DeleteTable", f"arn:aws:dynamodb:us-east-1:{_HUB_ACCOUNT_ID}:table/openci-tf-locks"),
    ("dynamodb:UpdateTable", f"arn:aws:dynamodb:us-east-1:{_HUB_ACCOUNT_ID}:table/openci-tf-run-registry"),
    ("s3:DeleteBucket", f"arn:aws:s3:::openci-tf-state-{_HUB_ACCOUNT_ID}"),
    ("s3:PutObject", f"arn:aws:s3:::openci-tf-tmp-{_HUB_ACCOUNT_ID}/artifact"),
    ("s3:PutObject", f"arn:aws:s3:::openci-tf-package-{_HUB_ACCOUNT_ID}/package.zip"),
    ("s3:DeleteObject", f"arn:aws:s3:::openci-tf-done-{_HUB_ACCOUNT_ID}/run/done"),
    ("iam:DeleteRole", f"arn:aws:iam::{_HUB_ACCOUNT_ID}:role/openci-tf-executor-poweruser"),
    ("iam:UpdateAssumeRolePolicy", f"arn:aws:iam::{_HUB_ACCOUNT_ID}:role/openci-tf-lambda-role"),
    ("lambda:DeleteFunction", f"arn:aws:lambda:us-east-1:{_HUB_ACCOUNT_ID}:function:openci-tf-init-job"),
    ("codebuild:DeleteProject", f"arn:aws:codebuild:us-east-1:{_HUB_ACCOUNT_ID}:project/openci-tf-worker"),
    ("states:DeleteStateMachine", f"arn:aws:states:us-east-1:{_HUB_ACCOUNT_ID}:stateMachine:openci-tf-codebuild"),
    ("ecr:DeleteRepository", f"arn:aws:ecr:us-east-1:{_HUB_ACCOUNT_ID}:repository/openci-tf"),
]


@pytest.mark.parametrize(("action", "resource"), _PROTECTED_HUB_CASES)
def test_poweruser_explicitly_denies_protected_hub_resources(
    poweruser_inline_policy: dict,
    action: str,
    resource: str,
) -> None:
    assert is_explicitly_denied(
        poweruser_inline_policy,
        action=action,
        resource=resource,
    )
    assert not evaluate_legacy_remote_poweruser_effective_policy(
        poweruser_inline_policy,
        action=action,
        resource=resource,
    )


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        ("s3:PutObject", "arn:aws:s3:::tenant-workload/app/data.json"),
        ("ec2:RunInstances", "arn:aws:ec2:us-east-1:222222222222:instance/*"),
        ("dynamodb:CreateTable", "arn:aws:dynamodb:us-east-1:222222222222:table/tenant-app"),
    ],
)
def test_poweruser_still_allows_ordinary_tenant_resources(
    poweruser_inline_policy: dict,
    action: str,
    resource: str,
) -> None:
    assert evaluate_legacy_remote_poweruser_effective_policy(
        poweruser_inline_policy,
        action=action,
        resource=resource,
    )


def test_poweruser_policy_has_no_permissions_boundary() -> None:
    source = Path("infra/modules/executor-poweruser/main.tf").read_text(encoding="utf-8")
    assert "permissions_boundary" not in source
    assert "executor_poweruser_permissions_boundary" not in source


def _inline_without_plan_time_iam_reads(inline_policy: dict) -> dict:
    weakened = policy_without_statement_sid(
        inline_policy, "TerraformPlanTimeIamReadsScoped"
    )
    return policy_without_statement_sid(weakened, "TerraformPlanTimeIamReadsWildcard")


@pytest.fixture(scope="module")
def poweruser_inline_policy() -> dict:
    return render_poweruser_inline_policy()


@pytest.fixture(scope="module")
def poweruser_boundary_policy() -> dict:
    return render_poweruser_boundary_policy()


def _effective(
    inline: dict,
    boundary: dict,
    *,
    action: str,
    resource: str,
    context: dict | None = None,
) -> bool:
    return evaluate_effective_policy(
        inline,
        boundary,
        action=action,
        resource=resource,
        context=context,
    )


def test_poweruser_rendered_policies_carry_no_lock_table_authority(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    """Decision 27: no DynamoDB statement remains in rendered executor policies."""
    import json as _json

    for policy in (poweruser_inline_policy, poweruser_boundary_policy):
        rendered = _json.dumps(policy)
        # The state lock table (<prefix>-tf-locks) is gone; the hub's internal
        # coordination table (<prefix>-locks) may still appear in deny lists.
        assert "tf-tf-locks" not in rendered
        assert "dynamodb:LeadingKeys" not in rendered
        for statement in policy["Statement"]:
            if statement.get("Effect") != "Allow":
                continue
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            assert not any(
                action.startswith("dynamodb:") for action in actions
            ), statement


@pytest.mark.parametrize(
    "action",
    [
        "s3:PutInventoryConfiguration",
        "s3:DeleteInventoryConfiguration",
        "s3:PutAnalyticsConfiguration",
        "s3:PutMetricsConfiguration",
        "s3:PutAccelerateConfiguration",
        "s3:PutBucketPolicy",
        "s3:DeleteBucket",
    ],
)
def test_poweruser_denies_state_bucket_control_plane_actions(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
    action: str,
) -> None:
    assert is_explicitly_denied(
        poweruser_inline_policy,
        action=action,
        resource=_STATE_BUCKET,
    )
    assert not _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action=action,
        resource=_STATE_BUCKET,
    )


@pytest.mark.parametrize(
    "action",
    [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    ],
)
def test_poweruser_allows_target_state_object_primitives(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
    action: str,
) -> None:
    assert not is_explicitly_denied(
        poweruser_inline_policy,
        action=action,
        resource=_TARGET_KEY,
    )
    assert _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action=action,
        resource=_TARGET_KEY,
    )


def test_poweruser_allows_list_bucket_with_target_prefix(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    assert not is_explicitly_denied(
        poweruser_inline_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_LIST_PREFIX,
    )
    assert _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_LIST_PREFIX,
    )


@pytest.mark.parametrize(
    "key",
    [
        _NON_TARGET_KEY,
        _SOURCE_KEY,
    ],
)
@pytest.mark.parametrize(
    "action",
    [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    ],
)
def test_poweruser_denies_object_primitives_outside_targets(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
    action: str,
    key: str,
) -> None:
    assert is_explicitly_denied(
        poweruser_inline_policy,
        action=action,
        resource=key,
    )
    assert not _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action=action,
        resource=key,
    )


_WORKLOAD_BUCKET_KEY = "arn:aws:s3:::customer-workload-bucket/app/data.json"


@pytest.mark.parametrize(
    "action",
    [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    ],
)
def test_poweruser_allows_object_primitives_on_workload_bucket(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
    action: str,
) -> None:
    assert not is_explicitly_denied(
        poweruser_inline_policy,
        action=action,
        resource=_WORKLOAD_BUCKET_KEY,
    )
    assert _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action=action,
        resource=_WORKLOAD_BUCKET_KEY,
    )


def test_poweruser_inline_policy_has_no_fabricated_resource_arn_deny(
    poweruser_inline_policy: dict,
) -> None:
    assert "DenyStateObjectOutsideTargets" not in {
        stmt.get("Sid") for stmt in poweruser_inline_policy["Statement"]
    }


def test_poweruser_denies_list_bucket_with_non_target_prefix(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    assert is_explicitly_denied(
        poweruser_inline_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_DEPLOY_LIST_PREFIX,
    )
    assert not _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_DEPLOY_LIST_PREFIX,
    )


def test_poweruser_denies_broad_list_bucket(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    assert is_explicitly_denied(
        poweruser_inline_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_BROAD_LIST,
    )
    assert not _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_BROAD_LIST,
    )


def test_poweruser_workload_iam_lifecycle_is_effectively_allowed(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    cases = (
        ("iam:CreateRole", f"arn:aws:iam::{_ACCOUNT_ID}:role/example"),
        ("iam:DeleteRole", f"arn:aws:iam::{_ACCOUNT_ID}:role/example"),
        ("iam:AttachRolePolicy", f"arn:aws:iam::{_ACCOUNT_ID}:role/example"),
        ("iam:DetachRolePolicy", f"arn:aws:iam::{_ACCOUNT_ID}:role/example"),
        ("iam:PassRole", f"arn:aws:iam::{_ACCOUNT_ID}:role/example"),
        (
            "iam:CreateInstanceProfile",
            f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
        ),
        (
            "iam:DeleteInstanceProfile",
            f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
        ),
        (
            "iam:AddRoleToInstanceProfile",
            f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
        ),
        (
            "iam:RemoveRoleFromInstanceProfile",
            f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
        ),
    )
    for action, resource in cases:
        assert not is_explicitly_denied(
            poweruser_inline_policy,
            action=action,
            resource=resource,
        )
        assert _effective(
            poweruser_inline_policy,
            poweruser_boundary_policy,
            action=action,
            resource=resource,
        )


def test_poweruser_workload_iam_lifecycle_denies_cross_account_resources(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    for action, resource in (
        ("iam:DetachRolePolicy", _FOREIGN_ROLE_ARN),
        ("iam:PassRole", _FOREIGN_ROLE_ARN),
        ("iam:RemoveRoleFromInstanceProfile", _FOREIGN_INSTANCE_PROFILE_ARN),
    ):
        assert is_explicitly_denied(
            poweruser_inline_policy,
            action=action,
            resource=resource,
        )
        assert not _effective(
            poweruser_inline_policy,
            poweruser_boundary_policy,
            action=action,
            resource=resource,
        )


def test_poweruser_iam_reads_are_effectively_allowed(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    cases = (
        ("iam:GetRole", "arn:aws:iam::222222222222:role/example"),
        ("iam:GetRolePolicy", "arn:aws:iam::222222222222:role/example"),
        ("iam:ListRolePolicies", "arn:aws:iam::222222222222:role/example"),
        ("iam:ListAttachedRolePolicies", "arn:aws:iam::222222222222:role/example"),
        (
            "iam:GetInstanceProfile",
            "arn:aws:iam::222222222222:instance-profile/example",
        ),
        ("iam:ListInstanceProfilesForRole", "arn:aws:iam::222222222222:role/example"),
        ("iam:ListRoles", "*"),
        ("iam:SimulatePrincipalPolicy", "*"),
    )
    for action, resource in cases:
        assert not is_explicitly_denied(
            poweruser_inline_policy,
            action=action,
            resource=resource,
        )
        assert _effective(
            poweruser_inline_policy,
            poweruser_boundary_policy,
            action=action,
            resource=resource,
        )


def test_poweruser_iam_reads_require_inline_allow_not_managed_baseline(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    weakened_inline = _inline_without_plan_time_iam_reads(poweruser_inline_policy)
    assert not poweruser_managed_policy_allows("iam:GetRole")
    assert not poweruser_managed_policy_allows("iam:GetInstanceProfile")
    assert not _effective(
        weakened_inline,
        poweruser_boundary_policy,
        action="iam:GetRole",
        resource=f"arn:aws:iam::{_ACCOUNT_ID}:role/example",
    )
    assert not _effective(
        weakened_inline,
        poweruser_boundary_policy,
        action="iam:GetInstanceProfile",
        resource=f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
    )
    assert _effective(
        weakened_inline,
        poweruser_boundary_policy,
        action="iam:ListRoles",
        resource="*",
    )


@pytest.mark.parametrize(
    "action,resource",
    [
        ("iam:GetRole", _FOREIGN_ROLE_ARN),
        ("iam:GetRolePolicy", _FOREIGN_ROLE_ARN),
        ("iam:ListRolePolicies", _FOREIGN_ROLE_ARN),
        ("iam:ListAttachedRolePolicies", _FOREIGN_ROLE_ARN),
        ("iam:GetInstanceProfile", _FOREIGN_INSTANCE_PROFILE_ARN),
        ("iam:ListInstanceProfilesForRole", _FOREIGN_ROLE_ARN),
    ],
)
def test_poweruser_iam_scoped_reads_deny_out_of_scope_accounts(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
    action: str,
    resource: str,
) -> None:
    scoped_only = {
        "Version": "2012-10-17",
        "Statement": [
            stmt
            for stmt in poweruser_inline_policy["Statement"]
            if stmt.get("Sid") == "TerraformPlanTimeIamReadsScoped"
        ],
    }
    assert not evaluate_inline_policy(
        scoped_only,
        action=action,
        resource=resource,
    )
    assert not _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action=action,
        resource=resource,
    )


def test_poweruser_boundary_alone_does_not_grant_scoped_iam_reads(
    poweruser_boundary_policy: dict,
) -> None:
    empty_inline: dict = {"Version": "2012-10-17", "Statement": []}
    for action, resource in (
        ("iam:GetRole", f"arn:aws:iam::{_ACCOUNT_ID}:role/example"),
        (
            "iam:GetInstanceProfile",
            f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
        ),
    ):
        assert not poweruser_managed_policy_allows(action)
        assert not _effective(
            empty_inline,
            poweruser_boundary_policy,
            action=action,
            resource=resource,
        )


def test_poweruser_managed_policy_alone_does_not_grant_scoped_iam_reads(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    weakened_inline = _inline_without_plan_time_iam_reads(poweruser_inline_policy)
    for action, resource in (
        ("iam:GetRole", f"arn:aws:iam::{_ACCOUNT_ID}:role/example"),
        (
            "iam:GetInstanceProfile",
            f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
        ),
    ):
        assert not poweruser_managed_policy_allows(action)
        assert not _effective(
            weakened_inline,
            poweruser_boundary_policy,
            action=action,
            resource=resource,
        )


def test_poweruser_managed_policy_alone_does_not_grant_fabricated_iam_reads() -> None:
    for action in (
        "iam:ListUsers",
        "iam:ListGroups",
        "iam:ListPolicies",
        "iam:GetAccountSummary",
        "iam:ListAccountAliases",
    ):
        assert not poweruser_managed_policy_allows(action)


def test_poweruser_denies_iam_reset_mutations(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
) -> None:
    assert is_explicitly_denied(
        poweruser_inline_policy,
        action="iam:ResetServiceSpecificCredential",
        resource="*",
    )
    assert not _effective(
        poweruser_inline_policy,
        poweruser_boundary_policy,
        action="iam:ResetServiceSpecificCredential",
        resource="*",
    )


@pytest.mark.parametrize(
    "sid,action,resource,context",
    [
        (
            "DenyIamAndCloudFormationUnconditionally",
            "iam:ResetServiceSpecificCredential",
            "*",
            None,
        ),
    ],
)
def test_negative_control_inline_deny_removal_allows_poweruser_baseline(
    poweruser_inline_policy: dict,
    poweruser_boundary_policy: dict,
    sid: str,
    action: str,
    resource: str,
    context: dict | None,
) -> None:
    weakened_inline = policy_without_statement_sid(poweruser_inline_policy, sid)
    assert not is_explicitly_denied(
        weakened_inline, action=action, resource=resource, context=context
    )
    if action.startswith("iam:"):
        return
    assert _effective(
        weakened_inline,
        poweruser_boundary_policy,
        action=action,
        resource=resource,
        context=context,
    )


def _legacy_remote_effective(
    inline: dict,
    *,
    action: str,
    resource: str,
    context: dict | None = None,
) -> bool:
    return evaluate_legacy_remote_poweruser_effective_policy(
        inline,
        action=action,
        resource=resource,
        context=context,
    )


@pytest.fixture(scope="module")
def legacy_remote_inline_policy() -> dict:
    return render_legacy_remote_inline_policy()


def test_legacy_remote_iam_reads_are_effectively_allowed(
    legacy_remote_inline_policy: dict,
) -> None:
    cases = (
        ("iam:GetRole", f"arn:aws:iam::{_ACCOUNT_ID}:role/openci-tf-tracer-euw1-role"),
        (
            "iam:GetRolePolicy",
            f"arn:aws:iam::{_ACCOUNT_ID}:role/openci-tf-tracer-euw1-role",
        ),
        (
            "iam:ListRolePolicies",
            f"arn:aws:iam::{_ACCOUNT_ID}:role/openci-tf-tracer-euw1-role",
        ),
        (
            "iam:ListAttachedRolePolicies",
            f"arn:aws:iam::{_ACCOUNT_ID}:role/openci-tf-tracer-euw1-role",
        ),
        (
            "iam:GetInstanceProfile",
            f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example",
        ),
        (
            "iam:ListInstanceProfilesForRole",
            f"arn:aws:iam::{_ACCOUNT_ID}:role/openci-tf-tracer-euw1-role",
        ),
        ("iam:ListRoles", "*"),
        ("iam:SimulatePrincipalPolicy", "*"),
    )
    for action, resource in cases:
        assert not is_explicitly_denied(
            legacy_remote_inline_policy,
            action=action,
            resource=resource,
        )
        assert _legacy_remote_effective(
            legacy_remote_inline_policy,
            action=action,
            resource=resource,
        )


def test_legacy_remote_iam_reads_require_inline_allow_not_managed_baseline(
    legacy_remote_inline_policy: dict,
) -> None:
    weakened_inline = _inline_without_plan_time_iam_reads(legacy_remote_inline_policy)
    assert not poweruser_managed_policy_allows("iam:GetRole")
    assert not _legacy_remote_effective(
        weakened_inline,
        action="iam:GetRole",
        resource=f"arn:aws:iam::{_ACCOUNT_ID}:role/openci-tf-tracer-euw1-role",
    )
    assert _legacy_remote_effective(
        weakened_inline,
        action="iam:ListRoles",
        resource="*",
    )


@pytest.mark.parametrize(
    "action,resource",
    [
        ("iam:GetRole", _FOREIGN_ROLE_ARN),
        ("iam:GetRolePolicy", _FOREIGN_ROLE_ARN),
        ("iam:ListRolePolicies", _FOREIGN_ROLE_ARN),
        ("iam:ListAttachedRolePolicies", _FOREIGN_ROLE_ARN),
        ("iam:GetInstanceProfile", _FOREIGN_INSTANCE_PROFILE_ARN),
        ("iam:ListInstanceProfilesForRole", _FOREIGN_ROLE_ARN),
    ],
)
def test_legacy_remote_iam_scoped_reads_deny_out_of_scope_accounts(
    legacy_remote_inline_policy: dict,
    action: str,
    resource: str,
) -> None:
    scoped_only = {
        "Version": "2012-10-17",
        "Statement": [
            stmt
            for stmt in legacy_remote_inline_policy["Statement"]
            if stmt.get("Sid") == "TerraformPlanTimeIamReadsScoped"
        ],
    }
    assert not evaluate_inline_policy(
        scoped_only,
        action=action,
        resource=resource,
    )
    assert not _legacy_remote_effective(
        legacy_remote_inline_policy,
        action=action,
        resource=resource,
    )


def test_legacy_remote_denies_iam_mutations(
    legacy_remote_inline_policy: dict,
) -> None:
    assert is_explicitly_denied(
        legacy_remote_inline_policy,
        action="iam:PassRole",
        resource="*",
    )
    assert not _legacy_remote_effective(
        legacy_remote_inline_policy,
        action="iam:PassRole",
        resource="*",
    )
