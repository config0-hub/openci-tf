# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory stand-in for the DynamoDB locks table conditional-write surface."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError


def _condition_failed(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, operation)


class FakeLocksTable:
    name = "locks"

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_calls = 0

    def put_item(self, *, Item: dict[str, Any], ConditionExpression: str, ExpressionAttributeValues: dict[str, Any]) -> None:
        self.put_calls += 1
        key = (Item["pk"], Item["sk"])
        current = self.items.get(key)
        if current is not None and current["expires_at"] >= ExpressionAttributeValues[":now"]:
            raise _condition_failed("PutItem")
        self.items[key] = dict(Item)

    def delete_item(self, *, Key: dict[str, str], ConditionExpression: str | None = None, ExpressionAttributeValues: dict[str, Any] | None = None) -> None:
        key = (Key["pk"], Key["sk"])
        current = self.items.get(key)
        if ConditionExpression is not None:
            if current is None or current.get("holder") != ExpressionAttributeValues[":holder"]:
                raise _condition_failed("DeleteItem")
        self.items.pop(key, None)
