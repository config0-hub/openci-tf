# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic evaluation tests for rendered executor-readonly IAM policy."""
from __future__ import annotations

import pytest

from tests.unit.iam_policy_evaluator import (
    _STATE_BUCKET,
    boundary_without_state_exclusions,
    evaluate_readonly_effective_policy,
    is_explicitly_denied,
    is_implicitly_denied_by_readonly_boundary,
    policy_without_statement_sid,
    readonly_managed_policy_allows,
    render_readonly_boundary_policy,
    render_readonly_inline_policy,
)

_ACCOUNT_ID = "222222222222"
_FOREIGN_ROLE_ARN = "arn:aws:iam::999999999999:role/example"
_TRACER_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/openci-tf-tracer-apne1-role"
_TARGET_KEY = f"{_STATE_BUCKET}/targets/org/repo/folder.tfstate"
_NON_TARGET_KEY = f"{_STATE_BUCKET}/deploy/terraform.tfstate"
_LIST_PREFIX = {"s3:prefix": ["targets/foo"]}
_UNLISTED_CONTROL_KEY = f"{_STATE_BUCKET}/unlisted-control/terraform.tfstate"
_SOURCE_KEY = f"{_STATE_BUCKET}/source/manifest.json"
_DEPLOY_LIST_PREFIX = {"s3:prefix": ["deploy/foo"]}
_BROAD_LIST: dict[str, list[str]] = {}
_WORKLOAD_BUCKET_KEY = "arn:aws:s3:::customer-workload-bucket/app/data.json"


@pytest.fixture(scope="module")
def readonly_inline_policy() -> dict:
    return render_readonly_inline_policy()


@pytest.fixture(scope="module")
def readonly_boundary_policy() -> dict:
    return render_readonly_boundary_policy()


def _effective(
    inline: dict,
    boundary: dict,
    *,
    action: str,
    resource: str,
    context: dict | None = None,
) -> bool:
    return evaluate_readonly_effective_policy(
        inline,
        boundary,
        action=action,
        resource=resource,
        context=context,
    )


def test_readonly_allows_target_state_object_primitives(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject"):
        assert _effective(
            readonly_inline_policy,
            readonly_boundary_policy,
            action=action,
            resource=_TARGET_KEY,
        )


def test_readonly_allows_list_bucket_with_target_prefix(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    assert _effective(
        readonly_inline_policy,
        readonly_boundary_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_LIST_PREFIX,
    )


@pytest.mark.parametrize(
    "key",
    [
        _NON_TARGET_KEY,
        _UNLISTED_CONTROL_KEY,
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
def test_readonly_denies_object_primitives_outside_targets(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
    action: str,
    key: str,
) -> None:
    if key in {_SOURCE_KEY, _NON_TARGET_KEY}:
        assert is_explicitly_denied(
            readonly_inline_policy,
            action=action,
            resource=key,
        )
    elif action == "s3:GetObject":
        assert is_implicitly_denied_by_readonly_boundary(
            readonly_inline_policy,
            readonly_boundary_policy,
            action=action,
            resource=key,
        )
    else:
        assert not readonly_managed_policy_allows(action)
    assert not _effective(
        readonly_inline_policy,
        readonly_boundary_policy,
        action=action,
        resource=key,
    )


def test_readonly_denies_list_bucket_with_non_target_prefix(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    assert is_explicitly_denied(
        readonly_inline_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_DEPLOY_LIST_PREFIX,
    )
    assert not _effective(
        readonly_inline_policy,
        readonly_boundary_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_DEPLOY_LIST_PREFIX,
    )


def test_readonly_denies_broad_list_bucket(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    assert is_explicitly_denied(
        readonly_inline_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_BROAD_LIST,
    )
    assert not _effective(
        readonly_inline_policy,
        readonly_boundary_policy,
        action="s3:ListBucket",
        resource=_STATE_BUCKET,
        context=_BROAD_LIST,
    )


def test_readonly_allows_object_reads_on_workload_bucket(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    assert _effective(
        readonly_inline_policy,
        readonly_boundary_policy,
        action="s3:GetObject",
        resource=_WORKLOAD_BUCKET_KEY,
    )


def test_readonly_boundary_excludes_state_bucket_from_broad_allow(
    readonly_boundary_policy: dict,
) -> None:
    broad = next(
        s for s in readonly_boundary_policy["Statement"] if s.get("Sid") == "BoundaryBroadWorkloadAllow"
    )
    not_resources = broad["NotResource"]
    assert _STATE_BUCKET in not_resources
    assert f"{_STATE_BUCKET}/*" in not_resources
    assert f"{_STATE_BUCKET}/target-connect-poweruser/*" in not_resources


def test_readonly_managed_policy_does_not_grant_writes() -> None:
    assert not readonly_managed_policy_allows("s3:PutObject")
    assert not readonly_managed_policy_allows("ec2:RunInstances")
    assert readonly_managed_policy_allows("s3:GetObject")
    assert readonly_managed_policy_allows("iam:GetRole")


def test_readonly_iam_reads_are_effectively_allowed(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    cases = (
        ("iam:GetRole", _TRACER_ROLE_ARN),
        ("iam:GetRolePolicy", _TRACER_ROLE_ARN),
        ("iam:ListRolePolicies", _TRACER_ROLE_ARN),
        ("iam:ListAttachedRolePolicies", _TRACER_ROLE_ARN),
        ("iam:GetInstanceProfile", f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/example"),
        ("iam:ListInstanceProfilesForRole", _TRACER_ROLE_ARN),
        ("iam:ListRoles", "*"),
        ("iam:SimulatePrincipalPolicy", "*"),
    )
    for action, resource in cases:
        assert not is_explicitly_denied(
            readonly_inline_policy,
            action=action,
            resource=resource,
        )
        assert _effective(
            readonly_inline_policy,
            readonly_boundary_policy,
            action=action,
            resource=resource,
        )


def test_readonly_iam_scoped_inline_grants_in_account_tracer_role(
    readonly_inline_policy: dict,
) -> None:
    from tests.unit.iam_policy_evaluator import evaluate_inline_policy

    scoped_only = {
        "Version": "2012-10-17",
        "Statement": [
            stmt
            for stmt in readonly_inline_policy["Statement"]
            if stmt.get("Sid") == "TerraformPlanTimeIamReadsScoped"
        ],
    }
    assert evaluate_inline_policy(
        scoped_only,
        action="iam:GetRole",
        resource=_TRACER_ROLE_ARN,
    )
    assert not evaluate_inline_policy(
        scoped_only,
        action="iam:GetRole",
        resource=_FOREIGN_ROLE_ARN,
    )


def test_readonly_still_denies_iam_mutations(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    for action in (
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:PutRolePolicy",
        "iam:PassRole",
    ):
        assert is_explicitly_denied(
            readonly_inline_policy,
            action=action,
            resource=_TRACER_ROLE_ARN,
        )
        assert not _effective(
            readonly_inline_policy,
            readonly_boundary_policy,
            action=action,
            resource=_TRACER_ROLE_ARN,
        )


def test_negative_control_boundary_state_exclusion_removal_allows_non_target_objects(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    weakened_boundary = boundary_without_state_exclusions(readonly_boundary_policy)
    assert _effective(
        readonly_inline_policy,
        weakened_boundary,
        action="s3:GetObject",
        resource=_UNLISTED_CONTROL_KEY,
    )


def test_negative_control_boundary_target_allow_removal_denies_target_objects(
    readonly_inline_policy: dict,
    readonly_boundary_policy: dict,
) -> None:
    weakened_boundary = policy_without_statement_sid(
        readonly_boundary_policy,
        "BoundaryTerraformTargetStateReadWrite",
    )
    assert is_implicitly_denied_by_readonly_boundary(
        readonly_inline_policy,
        weakened_boundary,
        action="s3:GetObject",
        resource=_TARGET_KEY,
    )
    assert not _effective(
        readonly_inline_policy,
        weakened_boundary,
        action="s3:GetObject",
        resource=_TARGET_KEY,
    )
