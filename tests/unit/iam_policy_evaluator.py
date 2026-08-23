# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal IAM inline-policy evaluator for executor role deny semantics."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_POWERUSER_MODULE = _REPO_ROOT / "infra/modules/executor-poweruser"
_READONLY_MODULE = _REPO_ROOT / "infra/modules/executor-readonly"
_LEGACY_REMOTE_MODULE = _REPO_ROOT / "infra/modules/target-connect"
_LOCK_TABLE = "arn:aws:dynamodb:us-east-1:222222222222:table/openci-tf-tf-locks"
_STATE_BUCKET = "arn:aws:s3:::openci-tf-state-222222222222"
_HUB_ACCOUNT_ID = "111111111111"
_PROTECTED_HUB_RESOURCES = [
    f"arn:aws:s3:::openci-tf-{bucket}-{_HUB_ACCOUNT_ID}{suffix}"
    for bucket in ("state", "tmp", "package", "done")
    for suffix in ("", "/*")
] + [
    f"arn:aws:dynamodb:us-east-1:{_HUB_ACCOUNT_ID}:table/openci-tf-locks",
    f"arn:aws:dynamodb:us-east-1:{_HUB_ACCOUNT_ID}:table/openci-tf-locks/index/*",
    f"arn:aws:dynamodb:us-east-1:{_HUB_ACCOUNT_ID}:table/openci-tf-run-registry",
    f"arn:aws:dynamodb:us-east-1:{_HUB_ACCOUNT_ID}:table/openci-tf-run-registry/index/*",
    f"arn:aws:lambda:us-east-1:{_HUB_ACCOUNT_ID}:function:openci-tf-init-job",
    f"arn:aws:codebuild:us-east-1:{_HUB_ACCOUNT_ID}:project/openci-tf-worker",
    f"arn:aws:states:us-east-1:{_HUB_ACCOUNT_ID}:stateMachine:openci-tf-codebuild",
    f"arn:aws:states:us-east-1:{_HUB_ACCOUNT_ID}:execution:openci-tf-codebuild:*",
    f"arn:aws:ecr:us-east-1:{_HUB_ACCOUNT_ID}:repository/openci-tf",
] + [
    f"arn:aws:iam::{_HUB_ACCOUNT_ID}:role/{name}"
    for name in (
        "openci-tf-hub-lambda-exec",
        "openci-tf-executor-readonly",
        "openci-tf-executor-poweruser",
        "openci-tf-executor-remote",
        "openci-tf-executor-local",
        "openci-tf-lambda-role",
        "openci-tf-api-lambda-role",
        "openci-tf-worker",
        "openci-tf-codebuild",
        "openci-tf-finalizer",
    )
]


def _action_matches(pattern: str, action: str) -> bool:
    return fnmatch(action, pattern)


def _resource_matches(pattern: str, resource: str) -> bool:
    return fnmatch(resource, pattern)


def _conditions_match(statement: dict[str, Any], context: dict[str, Any]) -> bool:
    conditions = statement.get("Condition") or {}
    if not conditions:
        return True
    for test, clauses in conditions.items():
        if test == "ForAllValues:StringEquals":
            key = context.get("dynamodb:LeadingKeys")
            if key is None:
                return False
            expected = clauses.get("dynamodb:LeadingKeys", [])
            expected_values = expected if isinstance(expected, list) else [expected]
            if not all(value in expected_values for value in key):
                return False
        elif test == "ForAllValues:StringLike":
            key = context.get("dynamodb:LeadingKeys")
            if key is None:
                return False
            patterns = clauses.get("dynamodb:LeadingKeys", [])
            if not all(
                any(fnmatch(value, pattern) for pattern in patterns) for value in key
            ):
                return False
        elif test == "ForAllValues:StringNotLike":
            key = context.get("dynamodb:LeadingKeys")
            if key is None:
                return False
            patterns = clauses.get("dynamodb:LeadingKeys", [])
            if not all(
                all(not fnmatch(value, pattern) for pattern in patterns)
                for value in key
            ):
                return False
        elif test == "StringEquals":
            for context_key, expected in clauses.items():
                value = context.get(context_key)
                if value is None:
                    return False
                values = value if isinstance(value, list) else [value]
                expected_values = expected if isinstance(expected, list) else [expected]
                if not all(item in expected_values for item in values):
                    return False
        elif test == "StringLike":
            for context_key, patterns in clauses.items():
                value = context.get(context_key)
                if value is None:
                    return False
                values = value if isinstance(value, list) else [value]
                pattern_list = patterns if isinstance(patterns, list) else [patterns]
                if not all(
                    any(fnmatch(item, pattern) for pattern in pattern_list)
                    for item in values
                ):
                    return False
        elif test == "StringNotLike":
            for context_key, patterns in clauses.items():
                value = context.get(context_key)
                if value is None:
                    continue
                values = value if isinstance(value, list) else [value]
                pattern_list = patterns if isinstance(patterns, list) else [patterns]
                if not all(
                    all(not fnmatch(item, pattern) for pattern in pattern_list)
                    for item in values
                ):
                    return False
        elif test == "ArnNotLike":
            for context_key, patterns in clauses.items():
                value = context.get(context_key)
                if value is None:
                    continue
                values = value if isinstance(value, list) else [value]
                pattern_list = patterns if isinstance(patterns, list) else [patterns]
                if not all(
                    all(not fnmatch(item, pattern) for pattern in pattern_list)
                    for item in values
                ):
                    return False
        elif test == "Null":
            for context_key, null_val in clauses.items():
                present = context_key in context and context[context_key] is not None
                if null_val == "true" and present:
                    return False
                if null_val == "false" and not present:
                    return False
        else:
            return False
    return True


def _normalize_policy_values(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return [value]
    return list(value)


def is_explicitly_denied(
    policy: dict[str, Any],
    *,
    action: str,
    resource: str,
    context: dict[str, Any] | None = None,
) -> bool:
    context = context or {}
    for statement in policy.get("Statement", []):
        if statement.get("Effect") != "Deny":
            continue
        actions = _normalize_policy_values(
            statement.get("Action") or statement.get("NotAction") or []
        )
        if "Resource" in statement:
            resources = _normalize_policy_values(statement["Resource"])
            resource_hit = any(
                _resource_matches(pattern, resource) for pattern in resources
            )
        elif "NotResource" in statement:
            resources = _normalize_policy_values(statement["NotResource"])
            resource_hit = not any(
                _resource_matches(pattern, resource) for pattern in resources
            )
        else:
            resource_hit = True
        if not _conditions_match(statement, context):
            continue
        if "Action" in statement:
            action_hit = any(_action_matches(pattern, action) for pattern in actions)
            applies = action_hit and resource_hit
        else:
            action_hit = not any(
                _action_matches(pattern, action) for pattern in actions
            )
            applies = action_hit and resource_hit
        if applies:
            return True
    return False


def evaluate_inline_policy(
    policy: dict[str, Any],
    *,
    action: str,
    resource: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """Return True when the policy contains a matching Allow statement."""
    context = context or {}
    allowed = False
    for statement in policy.get("Statement", []):
        effect = statement.get("Effect")
        actions = _normalize_policy_values(
            statement.get("Action") or statement.get("NotAction") or []
        )
        if "Resource" in statement:
            resources = _normalize_policy_values(statement["Resource"])
            resource_hit = any(
                _resource_matches(pattern, resource) for pattern in resources
            )
        elif "NotResource" in statement:
            resources = _normalize_policy_values(statement["NotResource"])
            resource_hit = not any(
                _resource_matches(pattern, resource) for pattern in resources
            )
        else:
            resource_hit = True
        if not _conditions_match(statement, context):
            continue

        if "Action" in statement:
            action_hit = any(_action_matches(pattern, action) for pattern in actions)
            applies = action_hit and resource_hit
        else:
            action_hit = not any(
                _action_matches(pattern, action) for pattern in actions
            )
            applies = action_hit and resource_hit

        if not applies:
            continue
        if effect == "Deny":
            return False
        if effect == "Allow":
            allowed = True
    return allowed


# AWS PowerUserAccess v12 excludes general iam:* and restores only service-linked
# role lifecycle plus iam:ListRoles. Plan-time scoped IAM reads are granted only by
# the executor-poweruser inline identity Allow, not by the managed policy.
_POWERUSER_MANAGED_IAM_EXCEPTIONS = frozenset(
    {
        "iam:CreateServiceLinkedRole",
        "iam:DeleteServiceLinkedRole",
        "iam:ListRoles",
    }
)


def poweruser_managed_policy_allows(action: str) -> bool:
    if action.startswith("iam:"):
        return action in _POWERUSER_MANAGED_IAM_EXCEPTIONS
    if action.startswith(("organizations:", "account:")):
        return False
    return True


def poweruser_baseline_allows(action: str) -> bool:
    """Backward-compatible alias for PowerUserAccess baseline modeling."""
    return poweruser_managed_policy_allows(action)


_READONLY_WRITE_PREFIXES = (
    "ec2:Run",
    "ec2:Terminate",
    "ec2:Create",
    "ec2:Delete",
    "ec2:Modify",
    "s3:Put",
    "s3:Delete",
    "dynamodb:Put",
    "dynamodb:Delete",
    "dynamodb:Update",
    "dynamodb:Create",
    "iam:Create",
    "iam:Delete",
    "iam:Put",
    "iam:Update",
    "iam:Attach",
    "iam:Detach",
    "iam:Add",
    "iam:Remove",
    "iam:Pass",
    "cloudformation:Create",
    "cloudformation:Delete",
    "cloudformation:Update",
)


def readonly_managed_policy_allows(action: str) -> bool:
    if any(action.startswith(prefix) for prefix in _READONLY_WRITE_PREFIXES):
        return False
    if action.startswith(("organizations:", "account:")):
        return False
    return True


def evaluate_readonly_effective_policy(
    inline_policy: dict[str, Any],
    boundary_policy: dict[str, Any],
    *,
    action: str,
    resource: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """Effective allow = intersection of inline, ReadOnlyAccess, and permissions boundary."""
    context = context or {}
    if is_explicitly_denied(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    if is_explicitly_denied(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    identity_allowed = evaluate_inline_policy(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ) or readonly_managed_policy_allows(action)
    if not identity_allowed:
        return False
    return evaluate_inline_policy(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    )


def is_implicitly_denied_by_readonly_boundary(
    inline_policy: dict[str, Any],
    boundary_policy: dict[str, Any],
    *,
    action: str,
    resource: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """True when identity would allow but the readonly boundary ceiling does not."""
    context = context or {}
    if is_explicitly_denied(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    if is_explicitly_denied(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    identity_allowed = evaluate_inline_policy(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ) or readonly_managed_policy_allows(action)
    if not identity_allowed:
        return False
    return not evaluate_inline_policy(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    )


def evaluate_effective_policy(
    inline_policy: dict[str, Any],
    boundary_policy: dict[str, Any],
    *,
    action: str,
    resource: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """Effective allow = intersection of inline, PowerUserAccess, and permissions boundary."""
    context = context or {}
    if is_explicitly_denied(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    if is_explicitly_denied(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    identity_allowed = evaluate_inline_policy(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ) or poweruser_managed_policy_allows(action)
    if not identity_allowed:
        return False
    return evaluate_inline_policy(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    )


def evaluate_legacy_remote_poweruser_effective_policy(
    inline_policy: dict[str, Any],
    *,
    action: str,
    resource: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """Effective allow for enable_apply legacy executor-remote = inline intersect PowerUserAccess."""
    context = context or {}
    if is_explicitly_denied(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    return evaluate_inline_policy(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ) or poweruser_managed_policy_allows(action)


def is_implicitly_denied_by_boundary(
    inline_policy: dict[str, Any],
    boundary_policy: dict[str, Any],
    *,
    action: str,
    resource: str,
    context: dict[str, Any] | None = None,
) -> bool:
    """True when identity would allow but the boundary ceiling does not."""
    context = context or {}
    if is_explicitly_denied(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    if is_explicitly_denied(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    ):
        return False
    identity_allowed = evaluate_inline_policy(
        inline_policy,
        action=action,
        resource=resource,
        context=context,
    ) or poweruser_managed_policy_allows(action)
    if not identity_allowed:
        return False
    return not evaluate_inline_policy(
        boundary_policy,
        action=action,
        resource=resource,
        context=context,
    )


def policy_without_statement_sid(policy: dict[str, Any], sid: str) -> dict[str, Any]:
    return {
        **policy,
        "Statement": [
            stmt for stmt in policy.get("Statement", []) if stmt.get("Sid") != sid
        ],
    }


def boundary_without_state_exclusions(
    boundary_policy: dict[str, Any],
    *,
    state_bucket_arn: str = _STATE_BUCKET,
) -> dict[str, Any]:
    """Negative control: broad boundary Allow no longer excludes the state bucket."""
    weakened = copy.deepcopy(boundary_policy)
    for statement in weakened.get("Statement", []):
        if statement.get("Sid") != "BoundaryBroadWorkloadAllow":
            continue
        not_resources = _normalize_policy_values(statement.get("NotResource") or [])
        statement["NotResource"] = [
            arn for arn in not_resources if not arn.startswith(state_bucket_arn)
        ]
    return weakened


def _extract_policy_object_literal(
    module_dir: Path, resource_type: str, resource_name: str
) -> str:
    text = (module_dir / "main.tf").read_text(encoding="utf-8")
    marker = f'resource "{resource_type}" "{resource_name}"'
    chunk = text.split(marker, 1)[1]
    start = chunk.index("policy = jsonencode(") + len("policy = jsonencode(")
    depth = 0
    for offset, char in enumerate(chunk[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return chunk[start : offset + 1]
    raise RuntimeError(
        f"could not extract policy jsonencode block for {resource_type}.{resource_name}"
    )


_ACCOUNT_ID = "222222222222"
_IAM_READ_SCOPED_ACTIONS = [
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListRolePolicies",
    "iam:ListAttachedRolePolicies",
    "iam:GetInstanceProfile",
    "iam:ListInstanceProfilesForRole",
]
_IAM_READ_SCOPED_RESOURCES = [
    f"arn:aws:iam::{_ACCOUNT_ID}:role/*",
    f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/*",
]
_IAM_READ_WILDCARD_ACTIONS = [
    "iam:ListRoles",
    "iam:SimulatePrincipalPolicy",
]
_IAM_WORKLOAD_ROLE_LIFECYCLE_ACTIONS = [
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:PassRole",
]
_IAM_WORKLOAD_INSTANCE_PROFILE_LIFECYCLE_ACTIONS = [
    "iam:CreateInstanceProfile",
    "iam:DeleteInstanceProfile",
    "iam:AddRoleToInstanceProfile",
    "iam:RemoveRoleFromInstanceProfile",
    "iam:TagInstanceProfile",
    "iam:UntagInstanceProfile",
]


def _substitute_policy_vars(block: str) -> str:
    block = block.replace("var.lock_table_arn", json.dumps(_LOCK_TABLE))
    block = block.replace("var.state_bucket_arn", json.dumps(_STATE_BUCKET))
    block = block.replace("var.enable_apply", "true")
    block = block.replace(
        "local.protected_hub_resource_arns",
        json.dumps(_PROTECTED_HUB_RESOURCES),
    )
    block = block.replace(
        "local.terraform_plan_time_iam_read_scoped_actions",
        json.dumps(_IAM_READ_SCOPED_ACTIONS),
    )
    block = block.replace(
        "local.terraform_plan_time_iam_read_scoped_resources",
        json.dumps(_IAM_READ_SCOPED_RESOURCES),
    )
    block = block.replace(
        "local.terraform_plan_time_iam_read_wildcard_actions",
        json.dumps(_IAM_READ_WILDCARD_ACTIONS),
    )
    block = block.replace(
        "local.terraform_workload_iam_role_lifecycle_actions",
        json.dumps(_IAM_WORKLOAD_ROLE_LIFECYCLE_ACTIONS),
    )
    block = block.replace(
        "local.terraform_workload_iam_instance_profile_lifecycle_actions",
        json.dumps(_IAM_WORKLOAD_INSTANCE_PROFILE_LIFECYCLE_ACTIONS),
    )
    block = block.replace(
        "local.target_iam_role_arns",
        json.dumps(f"arn:aws:iam::{_ACCOUNT_ID}:role/*"),
    )
    block = block.replace(
        "local.target_iam_instance_profile_arns",
        json.dumps(f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/*"),
    )
    block = block.replace(
        "${var.lock_table_arn}/index/*",
        json.dumps(f"{_LOCK_TABLE}/index/*"),
    )
    block = block.replace(
        "${var.state_bucket_arn}/targets/*",
        json.dumps(f"{_STATE_BUCKET}/targets/*"),
    )
    for suffix in (
        "source/*",
        "engine/*",
        "bootstrap/*",
        "foundation/*",
        "deploy/*",
        "target-connect/*",
        "target-connect-readonly/*",
        "target-connect-poweruser/*",
        "engine-00-bootstrap/*",
        "engine-02-deploy/*",
    ):
        block = block.replace(
            f"${{var.state_bucket_arn}}/{suffix}",
            json.dumps(f"{_STATE_BUCKET}/{suffix}"),
        )
    return block


def _terraform_binary() -> str:
    for candidate in ("terraform", "tofu"):
        if shutil.which(candidate):
            return candidate
    raise FileNotFoundError("neither terraform nor tofu found on PATH")


def _terraform_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.update(
        {
            "AWS_ACCESS_KEY_ID": "mock",
            "AWS_SECRET_ACCESS_KEY": "mock",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
    )
    return env


def _render_policy_block(block: str) -> dict[str, Any]:
    block = _substitute_policy_vars(block)
    work = Path(tempfile.mkdtemp())
    (work / "main.tf").write_text(
        f"""
terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }}
  }}
}}

provider "aws" {{
  region                      = "us-east-1"
  access_key                  = "mock"
  secret_key                  = "mock"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}}

locals {{
  rendered = jsonencode({block})
}}
""".strip(),
        encoding="utf-8",
    )

    init = subprocess.run(
        [_terraform_binary(), f"-chdir={work}", "init", "-backend=false", "-input=false"],
        capture_output=True,
        text=True,
        check=False,
        env=_terraform_env(),
    )
    if init.returncode != 0:
        raise RuntimeError(init.stderr or init.stdout)

    console = subprocess.run(
        [_terraform_binary(), f"-chdir={work}", "console", "-no-color"],
        input="jsonencode(jsondecode(local.rendered))\n",
        capture_output=True,
        text=True,
        check=False,
        env=_terraform_env(),
    )
    if console.returncode != 0:
        raise RuntimeError(console.stderr or console.stdout)
    policy = json.loads(console.stdout.strip())
    if isinstance(policy, str):
        policy = json.loads(policy)
    return policy


def render_poweruser_inline_policy() -> dict[str, Any]:
    """Render executor-poweruser inline policy via terraform console evaluation."""
    block = _extract_policy_object_literal(
        _POWERUSER_MODULE, "aws_iam_role_policy", "executor_poweruser"
    )
    return _render_policy_block(block)


def render_poweruser_boundary_policy() -> dict[str, Any]:
    """Compatibility identity policy for tests written before boundaries were rejected."""
    source = (_POWERUSER_MODULE / "main.tf").read_text(encoding="utf-8")
    if "executor_poweruser_permissions_boundary" in source:
        raise AssertionError("poweruser permissions boundaries are forbidden")
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
    }


def render_readonly_inline_policy() -> dict[str, Any]:
    """Render executor-readonly inline policy via terraform console evaluation."""
    block = _extract_policy_object_literal(
        _READONLY_MODULE, "aws_iam_role_policy", "executor_readonly"
    )
    return _render_policy_block(block)


def render_readonly_boundary_policy() -> dict[str, Any]:
    """Render executor-readonly permissions boundary via terraform console evaluation."""
    block = _extract_policy_object_literal(
        _READONLY_MODULE,
        "aws_iam_policy",
        "executor_readonly_permissions_boundary",
    )
    return _render_policy_block(block)


def render_legacy_remote_inline_policy() -> dict[str, Any]:
    """Render legacy executor-remote inline policy via terraform console evaluation."""
    block = _extract_policy_object_literal(
        _LEGACY_REMOTE_MODULE, "aws_iam_role_policy", "executor_remote"
    )
    return _render_policy_block(block)
