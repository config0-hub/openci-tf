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
        self.update_calls = 0

    def put_item(self, *, Item: dict[str, Any], ConditionExpression: str, ExpressionAttributeValues: dict[str, Any]) -> None:
        self.put_calls += 1
        key = (Item["pk"], Item["sk"])
        current = self.items.get(key)
        if current is not None and current["expires_at"] >= ExpressionAttributeValues[":now"]:
            raise _condition_failed("PutItem")
        self.items[key] = dict(Item)

    def update_item(
        self,
        *,
        Key: dict[str, str],
        UpdateExpression: str,
        ConditionExpression: str | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
        ReturnValues: str | None = None,
    ) -> dict[str, Any]:
        self.update_calls += 1
        values = ExpressionAttributeValues or {}
        key = (Key["pk"], Key["sk"])
        current = self.items.get(key)
        if "ADD version" in UpdateExpression and "holder = :holder" in UpdateExpression and ":version" not in values:
            now = values[":now"]
            if current is not None and current.get("holder") is not None and current.get("expires_at", 0) >= now:
                raise _condition_failed("UpdateItem")
            item = dict(current or {"pk": Key["pk"], "sk": Key["sk"], "version": 0})
            item["holder"] = values[":holder"]
            item["expires_at"] = values[":expires_at"]
            item["version"] = int(item.get("version", 0)) + int(values[":one"])
            self.items[key] = item
            return {"Attributes": dict(item)} if ReturnValues == "ALL_NEW" else {}
        if "ADD version" in UpdateExpression and ":version" in values:
            if (
                current is None
                or current.get("holder") != values[":holder"]
                or current.get("version") != values[":version"]
                or current.get("expires_at", 0) < values[":now"]
            ):
                raise _condition_failed("UpdateItem")
            item = dict(current)
            item["expires_at"] = values[":expires_at"]
            item["version"] = int(item.get("version", 0)) + int(values[":one"])
            self.items[key] = item
            return {"Attributes": dict(item)} if ReturnValues == "ALL_NEW" else {}
        if "REMOVE holder" in UpdateExpression:
            if current is None or current.get("holder") != values[":holder"]:
                raise _condition_failed("UpdateItem")
            item = dict(current)
            item["expires_at"] = values[":expired"]
            item.pop("holder", None)
            self.items[key] = item
            return {"Attributes": dict(item)} if ReturnValues == "ALL_NEW" else {}
        raise NotImplementedError(f"unsupported fake update: {UpdateExpression}")

    def delete_item(self, *, Key: dict[str, str], ConditionExpression: str | None = None, ExpressionAttributeValues: dict[str, Any] | None = None) -> None:
        key = (Key["pk"], Key["sk"])
        current = self.items.get(key)
        if ConditionExpression is not None:
            if current is None or current.get("holder") != ExpressionAttributeValues[":holder"]:
                raise _condition_failed("DeleteItem")
        self.items.pop(key, None)
