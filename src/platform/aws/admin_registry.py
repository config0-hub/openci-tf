# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded read access to settings registrations and active run locks."""
from __future__ import annotations

import os
import time
from typing import Any

from src.platform.aws.dynamo_codec import normalize_dynamo_item
from src.platform.aws.dynamo_resource import dynamo_table

DEFAULT_ADMIN_PAGE_SIZE = 25
MAX_ADMIN_PAGE_SIZE = 100
MAX_ADMIN_CURSOR_BYTES = 512


class AdminRegistryError(RuntimeError):
    """Raised when admin reference data is missing or malformed."""


class AdminCursorError(ValueError):
    """Raised when an admin page cursor is too large or structurally unsafe."""


def _validated_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor:
        raise AdminCursorError("invalid admin cursor")
    if len(cursor.encode("utf-8")) > MAX_ADMIN_CURSOR_BYTES:
        raise AdminCursorError("admin cursor exceeds maximum size")
    if any(ord(char) < 32 for char in cursor):
        raise AdminCursorError("invalid admin cursor")
    return cursor


def _table_from_env(variable: str):
    table_name = os.environ.get(variable)
    if not table_name:
        raise AdminRegistryError(f"{variable} is not configured")
    return dynamo_table(table_name)


def _bounded_limit(limit: int) -> int:
    return min(max(1, limit), MAX_ADMIN_PAGE_SIZE)


def _next_cursor(response: dict[str, Any]) -> str | None:
    last_key = response.get("LastEvaluatedKey")
    if not isinstance(last_key, dict):
        return None
    cursor = last_key.get("sk")
    return cursor if isinstance(cursor, str) and cursor else None


def _query_partition(
    *,
    table_variable: str,
    partition: str,
    limit: int,
    cursor: str | None,
    filter_expression: str | None = None,
    expression_names: dict[str, str] | None = None,
    extra_values: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    validated_cursor = _validated_cursor(cursor)
    values: dict[str, Any] = {":pk": partition}
    if extra_values:
        values.update(extra_values)
    query: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": values,
        "Limit": _bounded_limit(limit),
    }
    if validated_cursor:
        query["ExclusiveStartKey"] = {"pk": partition, "sk": validated_cursor}
    if filter_expression:
        query["FilterExpression"] = filter_expression
    if expression_names:
        query["ExpressionAttributeNames"] = expression_names

    response = _table_from_env(table_variable).query(**query)
    items: list[dict[str, Any]] = []
    for raw_item in response.get("Items", []):
        if not isinstance(raw_item, dict):
            raise AdminRegistryError("DynamoDB query returned a non-object item")
        item = normalize_dynamo_item(raw_item)
        if item is None:
            raise AdminRegistryError("DynamoDB query returned an empty item")
        items.append(item)
    return items, _next_cursor(response)


def _required_string(item: dict[str, Any], field: str, *, row_type: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise AdminRegistryError(f"{row_type} row has invalid {field}")
    return value


def list_repo_registrations(
    *,
    limit: int = DEFAULT_ADMIN_PAGE_SIZE,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """List repository registration rows without exposing secret references."""
    rows, next_cursor = _query_partition(
        table_variable="SETTINGS_TABLE_NAME",
        partition="repo",
        limit=limit,
        cursor=cursor,
    )
    repos: list[dict[str, Any]] = []
    for row in rows:
        trigger_id = _required_string(row, "sk", row_type="repo")
        repo_name = _required_string(row, "repo_name", row_type="repo")
        require_approval = row.get("require_approval", False)
        if not isinstance(require_approval, bool):
            raise AdminRegistryError("repo row has invalid require_approval")
        repos.append(
            {
                "repo_name": repo_name,
                "trigger_ids": [trigger_id],
                "require_approval": require_approval,
            }
        )
    return repos, next_cursor


def list_account_targets(
    *,
    limit: int = DEFAULT_ADMIN_PAGE_SIZE,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """List stored account identity and role-name fields only."""
    rows, next_cursor = _query_partition(
        table_variable="SETTINGS_TABLE_NAME",
        partition="account",
        limit=limit,
        cursor=cursor,
    )
    accounts: list[dict[str, Any]] = []
    for row in rows:
        accounts.append(
            {
                "alias": _required_string(row, "sk", row_type="account"),
                "account_id": _required_string(row, "account_id", row_type="account"),
                "role_name": _required_string(row, "role_name", row_type="account"),
            }
        )
    return accounts, next_cursor


def _lock_identity(sort_key: str) -> tuple[str, str]:
    parts = sort_key.split("/", 2)
    if len(parts) != 3 or not all(parts):
        raise AdminRegistryError("lock row has invalid sk")
    return f"{parts[0]}/{parts[1]}", parts[2]


def list_active_locks(
    *,
    limit: int = DEFAULT_ADMIN_PAGE_SIZE,
    cursor: str | None = None,
    now: int | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """List unexpired lock rows, defensively rechecking TTL after DynamoDB filtering."""
    current = int(time.time()) if now is None else now
    rows, next_cursor = _query_partition(
        table_variable="LOCKS_TABLE_NAME",
        partition="lock",
        limit=limit,
        cursor=cursor,
        filter_expression="attribute_exists(#expires_at) AND #expires_at > :now",
        expression_names={"#expires_at": "expires_at"},
        extra_values={":now": current},
    )
    locks: list[dict[str, Any]] = []
    for row in rows:
        expires_at = row.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise AdminRegistryError("lock row has invalid expires_at")
        if expires_at <= current:
            continue
        repo_name, folder = _lock_identity(_required_string(row, "sk", row_type="lock"))
        locks.append(
            {
                "repo_name": repo_name,
                "folder": folder,
                "holder_execution_id": _required_string(
                    row,
                    "holder_execution_id",
                    row_type="lock",
                ),
                "expires_at": expires_at,
            }
        )
    return locks, next_cursor
