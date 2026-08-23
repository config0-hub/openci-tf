# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed DynamoDB table access for registry and lock helpers."""
from __future__ import annotations

from typing import Any, Protocol, cast

import boto3


class DynamoTable(Protocol):
    name: str
    meta: Any

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def put_item(self, **Item: Any) -> dict[str, Any]: ...
    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...
    def query(self, **kwargs: Any) -> dict[str, Any]: ...


def dynamo_table(name: str) -> DynamoTable:
    resource = cast(Any, boto3.resource("dynamodb"))
    return cast(DynamoTable, resource.Table(name))


def dynamo_client() -> Any:
    """Return an unmodified low-level client for pre-serialized transactions."""
    return boto3.client("dynamodb")
