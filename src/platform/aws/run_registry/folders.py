"""Folder-level submission, attempt, and summary records for the run registry."""

from __future__ import annotations

import math
import time
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-not-found]

from .keys import (
    MAX_ATTEMPTS_PER_FOLDER,
    folder_attempt_sk,
    folder_submission_sk,
    folder_summary_sk,
    run_pk,
    terminal_rank,
)
from .step_index import validate_registry_step_index

from . import _shared
from ._shared import (
    _TERMINAL_RANK,
    RunRegistryError,
    _bound_outcome,
    _bound_text,
    _normalize,
    expire_ttl,
    is_expired,
)


def put_folder_submission(
    *,
    run_id: str,
    folder: str,
    account_id: str,
    execution_id: str,
    attempt: int,
    submitted_at: float,
    engine_execution_arn: str | None = None,
    codebuild_build_id: str | None = None,
) -> dict[str, Any]:
    """Persist immutable engine acceptance before any progress notification."""
    if not run_id or not folder or not execution_id:
        raise ValueError("submission acknowledgement requires run, folder, and execution ids")
    if attempt < 0 or attempt >= MAX_ATTEMPTS_PER_FOLDER:
        raise ValueError("submission acknowledgement attempt is outside history bounds")
    if not math.isfinite(submitted_at) or submitted_at < 0:
        raise ValueError("submission acknowledgement submitted_at must be finite")
    now = int(time.time())
    item: dict[str, Any] = {
        "pk": run_pk(run_id),
        "sk": folder_submission_sk(folder, attempt),
        "run_id": run_id,
        "folder": folder,
        "account_id": account_id,
        "execution_id": execution_id,
        "attempt": attempt,
        "status": "accepted",
        "submitted_at": str(submitted_at),
        "notification_status": "pending",
        "notification_failed": False,
        "updated_at": now,
        "expire_ttl": expire_ttl(now),
    }
    if engine_execution_arn:
        item["engine_execution_arn"] = engine_execution_arn
    if codebuild_build_id:
        item["codebuild_build_id"] = codebuild_build_id
    table = _shared._table()
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk)",
        )
        return item
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code != "ConditionalCheckFailedException":
            raise
    existing = _normalize(
        table.get_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            ConsistentRead=True,
        ).get("Item")
    )
    if not existing:
        raise RunRegistryError("submission acknowledgement conflict without record")
    replay_fields = {
        "run_id",
        "folder",
        "account_id",
        "execution_id",
        "attempt",
        "status",
        "engine_execution_arn",
    }
    for key in replay_fields:
        if existing.get(key) != item.get(key):
            raise ValueError(f"submission acknowledgement replay mismatch on {key}")
    existing_build_id = existing.get("codebuild_build_id")
    incoming_build_id = item.get("codebuild_build_id")
    if (
        existing_build_id is not None
        and incoming_build_id is not None
        and existing_build_id != incoming_build_id
    ):
        raise ValueError("submission acknowledgement replay mismatch on codebuild_build_id")
    return existing


def record_folder_submission_notification(
    *,
    run_id: str,
    folder: str,
    execution_id: str,
    attempt: int,
    notification_status: str,
    notification_error: str | None = None,
) -> None:
    """Record notification outcome without changing accepted submission status."""
    if notification_status not in {"succeeded", "failed", "skipped", "not_applicable"}:
        raise ValueError("invalid submission notification status")
    values: dict[str, Any] = {
        ":notification_status": notification_status,
        ":notification_failed": notification_status == "failed",
        ":updated": int(time.time()),
        ":execution_id": execution_id,
    }
    expression = (
        "SET notification_status = :notification_status, "
        "notification_failed = :notification_failed, updated_at = :updated"
    )
    bounded_error = _bound_text(notification_error, label="notification_error")
    if bounded_error is not None:
        expression += ", notification_error = :notification_error"
        values[":notification_error"] = bounded_error
    _shared._table().update_item(
        Key={"pk": run_pk(run_id), "sk": folder_submission_sk(folder, attempt)},
        UpdateExpression=expression,
        ConditionExpression=(
            "attribute_exists(pk) AND execution_id = :execution_id AND #status = :accepted"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={**values, ":accepted": "accepted"},
    )


def put_folder_attempt(
    *,
    run_id: str,
    folder: str,
    account_id: str,
    execution_id: str,
    attempt: int,
    status: str,
    manifest_s3_uri: str | None = None,
    manifest_sha256: str | None = None,
    outcome: dict[str, Any] | None = None,
    deadline_at: str | None = None,
    drift_detected: bool | None = None,
    step_index: int = 1,
) -> None:
    if drift_detected is not None and type(drift_detected) is not bool:
        raise ValueError("drift_detected must be a boolean")
    api_step_index = validate_registry_step_index(step_index)
    now = int(time.time())
    ttl = expire_ttl(now)
    item: dict[str, Any] = {
        "pk": run_pk(run_id),
        "sk": folder_attempt_sk(folder, attempt),
        "run_id": run_id,
        "folder": folder,
        "account_id": account_id,
        "execution_id": execution_id,
        "attempt": attempt,
        "status": status,
        "step_index": api_step_index,
        "updated_at": now,
        "expire_ttl": ttl,
    }
    if deadline_at is not None:
        item["deadline_at"] = deadline_at
    if manifest_s3_uri:
        item["manifest_s3_uri"] = manifest_s3_uri
    if manifest_sha256:
        item["manifest_sha256"] = manifest_sha256
    if drift_detected is not None:
        item["drift_detected"] = drift_detected
    bounded = _bound_outcome(outcome)
    if bounded:
        item["outcome"] = bounded
    table = _shared._table()
    rank = terminal_rank(status)
    summary_values: dict[str, Any] = {
        ":folder": folder,
        ":account_id": account_id,
        ":execution_id": execution_id,
        ":attempt": attempt,
        ":status": status,
        ":step_index": api_step_index,
        ":updated": now,
        ":ttl": ttl,
        ":rank": rank,
        ":terminal": _TERMINAL_RANK,
    }
    summary_expression = (
        "SET folder = :folder, account_id = :account_id, execution_id = :execution_id, "
        "#attempt = :attempt, #status = :status, step_index = :step_index, "
        "updated_at = :updated, expire_ttl = :ttl, status_rank = :rank"
    )
    if deadline_at is not None:
        summary_expression += ", deadline_at = :deadline"
        summary_values[":deadline"] = deadline_at
    if manifest_s3_uri:
        summary_expression += ", manifest_s3_uri = :manifest"
        summary_values[":manifest"] = manifest_s3_uri
    if manifest_sha256:
        summary_expression += ", manifest_sha256 = :digest"
        summary_values[":digest"] = manifest_sha256
    if drift_detected is not None:
        summary_expression += ", drift_detected = :drift_detected"
        summary_values[":drift_detected"] = drift_detected
    summary_condition = (
        "attribute_not_exists(#attempt) OR ("
        ":attempt > #attempt OR "
        "(:attempt = #attempt AND ("
        "attribute_not_exists(status_rank) OR "
        "(status_rank < :terminal AND :rank >= status_rank) OR "
        "(:rank = status_rank AND #status = :status)"
        "))"
        ")"
    )
    try:
        _shared.transact_write_items(
            _shared.dynamo_client(),
            transact_items=[
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": item,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": {"pk": run_pk(run_id), "sk": folder_summary_sk(folder)},
                        "UpdateExpression": summary_expression,
                        "ConditionExpression": summary_condition,
                        "ExpressionAttributeNames": {
                            "#attempt": "attempt",
                            "#status": "status",
                        },
                        "ExpressionAttributeValues": summary_values,
                    }
                },
            ],
        )
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code not in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        }:
            raise
        existing = _normalize(
            table.get_item(
                Key={"pk": run_pk(run_id), "sk": folder_attempt_sk(folder, attempt)},
                ConsistentRead=True,
            ).get("Item")
        )
        if not existing or existing.get("execution_id") != execution_id:
            raise
        if manifest_sha256 and existing.get("manifest_sha256") not in {
            None,
            manifest_sha256,
        }:
            raise
        for key, value in item.items():
            if key in {"updated_at", "expire_ttl", "outcome"}:
                continue
            if key == "status" and terminal_rank(str(existing.get("status") or "")) == rank:
                continue
            if key == "step_index" and existing.get(key) is None:
                continue
            if existing.get(key) != value:
                raise ValueError(f"attempt item replay mismatch on {key}")


def _upsert_folder_summary(
    *,
    run_id: str,
    folder: str,
    account_id: str,
    execution_id: str,
    attempt: int,
    status: str,
    manifest_s3_uri: str | None,
    now: int,
    ttl: int,
) -> None:
    rank = terminal_rank(status)
    values: dict[str, Any] = {
        ":folder": folder,
        ":account_id": account_id,
        ":execution_id": execution_id,
        ":attempt": attempt,
        ":status": status,
        ":updated": now,
        ":ttl": ttl,
        ":rank": rank,
        ":terminal": _TERMINAL_RANK,
    }
    expression = (
        "SET folder = :folder, account_id = :account_id, execution_id = :execution_id, "
        "#attempt = :attempt, #status = :status, updated_at = :updated, expire_ttl = :ttl, status_rank = :rank"
    )
    if manifest_s3_uri:
        expression += ", manifest_s3_uri = :manifest"
        values[":manifest"] = manifest_s3_uri
    condition = (
        "attribute_not_exists(#attempt) OR ("
        ":attempt > #attempt OR "
        "(:attempt = #attempt AND ("
        "attribute_not_exists(status_rank) OR "
        "(status_rank < :terminal AND :rank >= status_rank) OR "
        "(:rank = status_rank AND #status = :status)"
        "))"
        ")"
    )
    _shared._table().update_item(
        Key={"pk": run_pk(run_id), "sk": folder_summary_sk(folder)},
        UpdateExpression=expression,
        ConditionExpression=condition,
        ExpressionAttributeNames={"#attempt": "attempt", "#status": "status"},
        ExpressionAttributeValues=values,
    )


def put_folder_record(
    *,
    run_id: str,
    folder: str,
    account_id: str,
    execution_id: str,
    attempt: int,
    status: str,
    manifest_s3_uri: str | None = None,
    manifest_sha256: str | None = None,
    outcome: dict[str, Any] | None = None,
    deadline_at: str | None = None,
    drift_detected: bool | None = None,
    step_index: int = 1,
) -> None:
    if attempt >= MAX_ATTEMPTS_PER_FOLDER:
        raise RunRegistryError("folder attempt history exceeds bound")
    put_folder_attempt(
        run_id=run_id,
        folder=folder,
        account_id=account_id,
        execution_id=execution_id or f"skipped-{folder}",
        attempt=attempt,
        status=status,
        manifest_s3_uri=manifest_s3_uri,
        manifest_sha256=manifest_sha256,
        outcome=outcome,
        deadline_at=deadline_at,
        drift_detected=drift_detected,
        step_index=step_index,

    )


def list_folder_records(run_id: str, *, max_items: int = 100) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
        "ExpressionAttributeValues": {":pk": run_pk(run_id), ":prefix": "folder#"},
    }
    while True:
        response = _shared._table().query(**query_kwargs)
        for item in response.get("Items", []):
            normalized = _normalize(item)
            if not normalized or is_expired(normalized):
                continue
            sk = str(normalized.get("sk", ""))
            if "#attempt#" in sk:
                continue
            summaries.append(normalized)
            if len(summaries) >= max_items:
                return summaries
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    return summaries


def get_folder_record(run_id: str, folder: str) -> dict[str, Any] | None:
    item = _normalize(
        _shared._table()
        .get_item(Key={"pk": run_pk(run_id), "sk": folder_summary_sk(folder)})
        .get("Item")
    )
    if not item or is_expired(item):
        return None
    return item


def get_folder_attempt(run_id: str, folder: str, attempt: int) -> dict[str, Any] | None:
    item = _normalize(
        _shared._table()
        .get_item(
            Key={"pk": run_pk(run_id), "sk": folder_attempt_sk(folder, attempt)},
            ConsistentRead=True,
        )
        .get("Item")
    )
    if not item or is_expired(item):
        return None
    return item
