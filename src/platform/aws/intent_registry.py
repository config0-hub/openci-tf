# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DynamoDB persistence for apply/destroy intent tokens."""
from __future__ import annotations

import os
import time
from typing import Any

from botocore.exceptions import ClientError

from src.platform.aws.dynamo_codec import normalize_dynamo_item
from src.platform.aws.dynamo_resource import dynamo_table


class IntentRegistryError(RuntimeError):
    pass


class IntentTokenConflictError(IntentRegistryError):
    pass


def _table():
    name = os.environ.get("RUN_REGISTRY_TABLE_NAME")
    if not name:
        raise IntentRegistryError("RUN_REGISTRY_TABLE_NAME is not configured")
    return dynamo_table(name)


def intent_pk(token: str) -> str:
    return f"intent#{token}"


def intent_sk() -> str:
    return "meta"


def put_intent_record(record: dict[str, Any]) -> None:
    item = {
        "pk": intent_pk(str(record["token"])),
        "sk": intent_sk(),
        "expire_ttl": int(record["expires_at"]) + 3600,
        "created_at": int(time.time()),
        **record,
    }
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")


def get_intent_record(token: str) -> dict[str, Any] | None:
    item = normalize_dynamo_item(
        _table().get_item(Key={"pk": intent_pk(token), "sk": intent_sk()}, ConsistentRead=True).get("Item")
    )
    if not item:
        return None
    return item


def update_intent_comment_metadata(
    token: str,
    *,
    requested_comment_id: int | None = None,
    requested_comment_body: str | None = None,
    intent_comment_id: int | None = None,
) -> None:
    values: dict[str, Any] = {}
    parts: list[str] = []
    if isinstance(requested_comment_id, int):
        values[":requested_comment_id"] = requested_comment_id
        parts.append("requested_comment_id = :requested_comment_id")
    if isinstance(requested_comment_body, str):
        values[":requested_comment_body"] = requested_comment_body
        parts.append("requested_comment_body = :requested_comment_body")
    if isinstance(intent_comment_id, int):
        values[":intent_comment_id"] = intent_comment_id
        parts.append("intent_comment_id = :intent_comment_id")
    if not parts:
        return
    _table().update_item(
        Key={"pk": intent_pk(token), "sk": intent_sk()},
        UpdateExpression="SET " + ", ".join(parts),
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(pk)",
    )


def mark_intent_record_used(
    token: str,
    *,
    trigger_id: str,
    pr_number: int,
    now: int | None = None,
) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    try:
        response = _table().update_item(
            Key={"pk": intent_pk(token), "sk": intent_sk()},
            UpdateExpression="SET used = :used",
            ConditionExpression=(
                "attribute_exists(pk) AND used = :false AND expires_at > :now "
                "AND trigger_id = :trigger_id AND pr_number = :pr_number"
            ),
            ExpressionAttributeValues={
                ":used": True,
                ":false": False,
                ":now": current,
                ":trigger_id": trigger_id,
                ":pr_number": pr_number,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise IntentTokenConflictError("token is missing, expired, or already used") from error
        raise
    item = normalize_dynamo_item(response.get("Attributes"))
    if not item:
        raise IntentRegistryError("token update returned no attributes")
    return item
