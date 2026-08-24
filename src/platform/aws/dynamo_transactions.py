# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DynamoDB transaction helpers using the low-level client and TypeSerializer."""
from __future__ import annotations

from typing import Any

from boto3.dynamodb.types import TypeSerializer

from src.platform.aws.dynamo_resource import dynamo_client

_serializer = TypeSerializer()


def serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _serializer.serialize(value) for key, value in item.items()}


def serialize_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _serializer.serialize(value) for key, value in values.items()}


def transact_write_items(*, transact_items: list[dict[str, Any]]) -> None:
    """Execute transact_write_items with native Python attribute values."""
    encoded: list[dict[str, Any]] = []
    for item in transact_items:
        operation = next(iter(item))
        payload = dict(item[operation])
        if "Key" in payload:
            payload["Key"] = serialize_item(payload["Key"])
        if "Item" in payload:
            payload["Item"] = serialize_item(payload["Item"])
        if "ExpressionAttributeValues" in payload:
            payload["ExpressionAttributeValues"] = serialize_values(payload["ExpressionAttributeValues"])
        encoded.append({operation: payload})
    dynamo_client().transact_write_items(TransactItems=encoded)
