# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-folder target-session policy rendering."""
from __future__ import annotations

import json
import re

from src.core.errors import ConfigResolutionError
from src.domain.run.folder_id import decode_folder_id, encode_folder_id

# STS packed policy quota is ~10% tighter than raw JSON chars (measured live:
# 1963 chars rendered as 109% packed for terraform/primary/ap-northeast-1/01-vpc).
MAX_SESSION_POLICY_CHARS = 1800

_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")
_POLICY_GLOB_CHARS = frozenset("*?[]")
_READ_ACTIONS = frozenset({"plan", "drift", "report", "plan_destroy"})
_MUTATION_ACTIONS = frozenset({"apply", "destroy"})


def target_state_key(repo_name: str, folder: str) -> str:
    """Return the checked-in backend key layout for one repository folder."""
    if not _REPO_NAME.fullmatch(repo_name):
        raise ConfigResolutionError("repo_name is unsafe for target state policy interpolation")
    normalized_folder = decode_folder_id(encode_folder_id(folder))
    interpolated = f"{repo_name}/{normalized_folder}"
    if any(character in interpolated for character in _POLICY_GLOB_CHARS):
        raise ConfigResolutionError(
            "folder or repository contains IAM wildcard characters"
        )
    return f"targets/{interpolated}.tfstate"


def _state_actions(action: str) -> list[str]:
    if action in _READ_ACTIONS:
        return ["s3:GetObject"]
    if action in _MUTATION_ACTIONS:
        return ["s3:GetObject", "s3:PutObject"]
    raise ConfigResolutionError(f"unsupported target-session action: {action}")


def _lock_write_keys(action: str, lock_id: str) -> list[str]:
    if action in _READ_ACTIONS:
        return [lock_id]
    if action in _MUTATION_ACTIONS:
        return [lock_id, f"{lock_id}-md5"]
    raise ConfigResolutionError(f"unsupported target-session action: {action}")


def _lock_statements(
    *,
    action: str,
    lock_table_arn: str,
    read_lock_keys: list[str],
    write_lock_keys: list[str],
) -> list[dict[str, object]]:
    if action in _MUTATION_ACTIONS and read_lock_keys == write_lock_keys:
        return [
            {
                "Effect": "Allow",
                "Action": [
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                "Resource": lock_table_arn,
                "Condition": {
                    "ForAllValues:StringEquals": {
                        "dynamodb:LeadingKeys": write_lock_keys
                    }
                },
            }
        ]
    return [
        {
            "Effect": "Allow",
            "Action": "dynamodb:GetItem",
            "Resource": lock_table_arn,
            "Condition": {
                "ForAllValues:StringEquals": {
                    "dynamodb:LeadingKeys": read_lock_keys
                }
            },
        },
        {
            "Effect": "Allow",
            "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem"],
            "Resource": lock_table_arn,
            "Condition": {
                "ForAllValues:StringEquals": {
                    "dynamodb:LeadingKeys": write_lock_keys
                }
            },
        },
    ]


def render_target_session_policy(
    *,
    account_id: str,
    repo_name: str,
    folder: str,
    action: str,
    project_name: str,
    region: str,
) -> str:
    """Render the session-policy intersection that narrows only backend authority."""
    if len(account_id) != 12 or not account_id.isdecimal():
        raise ConfigResolutionError("invalid frozen account_id for target session")
    if not _PROJECT_NAME.fullmatch(project_name):
        raise ConfigResolutionError("invalid project name for target session")
    if not _REGION.fullmatch(region):
        raise ConfigResolutionError("invalid AWS region for target session")

    state_key = target_state_key(repo_name, folder)
    bucket_name = f"{project_name}-state-{account_id}"
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    object_arn = f"{bucket_arn}/{state_key}"
    lock_table_arn = (
        f"arn:aws:dynamodb:{region}:{account_id}:table/{project_name}-tf-locks"
    )
    lock_id = f"{bucket_name}/{state_key}"
    read_lock_keys = [lock_id, f"{lock_id}-md5"]
    write_lock_keys = _lock_write_keys(action, lock_id)

    statements: list[dict[str, object]] = [
        {
            "Effect": "Allow",
            "Action": "*",
            "NotResource": [
                f"{bucket_arn}*",
                f"{lock_table_arn}*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": _state_actions(action),
            "Resource": object_arn,
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation", "s3:GetBucketVersioning"],
            "Resource": bucket_arn,
        },
        {
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": bucket_arn,
            "Condition": {"StringEquals": {"s3:prefix": state_key}},
        },
        *_lock_statements(
            action=action,
            lock_table_arn=lock_table_arn,
            read_lock_keys=read_lock_keys,
            write_lock_keys=write_lock_keys,
        ),
        {
            "Effect": "Allow",
            "Action": "dynamodb:DescribeTable",
            "Resource": lock_table_arn,
        },
    ]

    policy = {
        "Version": "2012-10-17",
        "Statement": statements,
    }
    rendered = json.dumps(policy, separators=(",", ":"), sort_keys=True)
    if len(rendered) > MAX_SESSION_POLICY_CHARS:
        raise ConfigResolutionError(
            f"target session policy exceeds {MAX_SESSION_POLICY_CHARS}-character STS limit"
        )
    return rendered
