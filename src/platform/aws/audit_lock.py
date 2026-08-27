# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Short-lived per-PR lock serializing durable audit comment read-modify-write."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, cast

import boto3
from botocore.exceptions import ClientError

from src.core.errors import LockHeldError

AUDIT_LOCK_TTL_SECONDS = 60


class AuditLockVersionError(RuntimeError):
    """Raised when DynamoDB returns an invalid audit lock version value."""


def locks_table() -> Any:
    """Return the DynamoDB locks table named by LOCKS_TABLE_NAME."""
    name = os.environ.get("LOCKS_TABLE_NAME")
    if not name:
        raise RuntimeError("LOCKS_TABLE_NAME is not configured")
    return cast(Any, boto3.resource("dynamodb")).Table(name)


def _key(repo: str, pr_number: int) -> dict[str, str]:
    return {"pk": "audit-lock", "sk": f"{repo}#pr-{pr_number}"}


def _version(attributes: dict[str, Any] | None, repo: str, pr_number: int) -> int:
    raw = (attributes or {}).get("version")
    if isinstance(raw, bool):
        raise AuditLockVersionError(
            f"audit lock for {repo}#{pr_number} returned no integer version"
        )
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Decimal) and raw == raw.to_integral_value():
        return int(raw)
    raise AuditLockVersionError(
        f"audit lock for {repo}#{pr_number} returned no integer version"
    )


def acquire(
    table: Any,
    repo: str,
    pr_number: int,
    holder: str,
    now: int,
    ttl: int = AUDIT_LOCK_TTL_SECONDS,
) -> int:
    """Take the PR audit lock and return its monotonically increasing version."""
    if ttl <= 0:
        raise ValueError("audit lock ttl must be positive")
    try:
        response = table.update_item(
            Key=_key(repo, pr_number),
            UpdateExpression=(
                "SET holder = :holder, expires_at = :expires_at ADD version :one"
            ),
            ConditionExpression=(
                "attribute_not_exists(pk) OR attribute_not_exists(holder) OR expires_at < :now"
            ),
            ExpressionAttributeValues={
                ":holder": holder,
                ":expires_at": now + ttl,
                ":now": now,
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise LockHeldError(f"audit lock held for {repo}#{pr_number}") from error
    return _version(response.get("Attributes"), repo, pr_number)


def fence(
    table: Any,
    repo: str,
    pr_number: int,
    holder: str,
    version: int,
    now: int,
    ttl: int = AUDIT_LOCK_TTL_SECONDS,
) -> int:
    """Bump the lock version only when this holder still owns the unchanged live lease."""
    if ttl <= 0:
        raise ValueError("audit lock ttl must be positive")
    try:
        response = table.update_item(
            Key=_key(repo, pr_number),
            UpdateExpression="SET expires_at = :expires_at ADD version :one",
            ConditionExpression=(
                "holder = :holder AND version = :version AND expires_at >= :now"
            ),
            ExpressionAttributeValues={
                ":holder": holder,
                ":version": version,
                ":expires_at": now + ttl,
                ":now": now,
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise LockHeldError(f"audit lock fence failed for {repo}#{pr_number}") from error
    return _version(response.get("Attributes"), repo, pr_number)


def release(table: Any, repo: str, pr_number: int, holder: str) -> None:
    """Release only the named holder while preserving the lock version fence."""
    try:
        table.update_item(
            Key=_key(repo, pr_number),
            UpdateExpression="SET expires_at = :expired REMOVE holder",
            ConditionExpression="holder = :holder",
            ExpressionAttributeValues={":holder": holder, ":expired": 0},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
