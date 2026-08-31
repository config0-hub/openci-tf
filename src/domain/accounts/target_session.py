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
_STATE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
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


def resolve_effective_state_location(
    *,
    account_id: str,
    repo_name: str,
    folder: str,
    project_name: str,
    state_bucket: str = "",
    state_key: str = "",
) -> tuple[str, str]:
    """Return the (bucket, key) pair one run's terraform backend is allowed to use.

    A folder config naming ``state_bucket``/``state_key`` pins the exact state
    object (shared-state repositories). Otherwise the conventional per-account
    bucket and per-folder key apply.
    """
    if bool(state_bucket) != bool(state_key):
        raise ConfigResolutionError(
            "state_bucket and state_key must be set together"
        )
    if state_bucket:
        if not _STATE_BUCKET.fullmatch(state_bucket):
            raise ConfigResolutionError(
                "state_bucket is unsafe for target state policy interpolation"
            )
        if any(character in state_key for character in _POLICY_GLOB_CHARS):
            raise ConfigResolutionError(
                "state_key contains IAM wildcard characters"
            )
        return state_bucket, state_key
    return (
        f"{project_name}-state-{account_id}",
        target_state_key(repo_name, folder),
    )


def _state_actions(action: str) -> list[str]:
    if action in _READ_ACTIONS:
        return ["s3:GetObject"]
    if action in _MUTATION_ACTIONS:
        return ["s3:GetObject", "s3:PutObject"]
    raise ConfigResolutionError(f"unsupported target-session action: {action}")


def _lock_object_actions(action: str) -> list[str]:
    # The S3 native lock file lives beside the state object; every locked run
    # creates and removes it, so the read lane also needs put/delete on the
    # lock object (and only on the lock object).
    if action in _READ_ACTIONS or action in _MUTATION_ACTIONS:
        return ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    raise ConfigResolutionError(f"unsupported target-session action: {action}")


def render_target_session_policy(
    *,
    account_id: str,
    repo_name: str,
    folder: str,
    action: str,
    project_name: str,
    state_bucket: str = "",
    state_key: str = "",
) -> str:
    """Render the session-policy intersection that narrows only backend authority."""
    if len(account_id) != 12 or not account_id.isdecimal():
        raise ConfigResolutionError("invalid frozen account_id for target session")
    if not _PROJECT_NAME.fullmatch(project_name):
        raise ConfigResolutionError("invalid project name for target session")

    bucket_name, effective_key = resolve_effective_state_location(
        account_id=account_id,
        repo_name=repo_name,
        folder=folder,
        project_name=project_name,
        state_bucket=state_bucket,
        state_key=state_key,
    )
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    object_arn = f"{bucket_arn}/{effective_key}"
    lock_object_key = f"{effective_key}.tflock"
    lock_object_arn = f"{bucket_arn}/{lock_object_key}"

    statements: list[dict[str, object]] = [
        {
            "Effect": "Allow",
            "Action": "*",
            "NotResource": [
                f"{bucket_arn}*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": _state_actions(action),
            "Resource": object_arn,
        },
        {
            "Effect": "Allow",
            "Action": _lock_object_actions(action),
            "Resource": lock_object_arn,
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
            "Condition": {
                "StringEquals": {"s3:prefix": [effective_key, lock_object_key]}
            },
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
