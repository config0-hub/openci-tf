# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run-level writes and reads for the DynamoDB run registry."""

from __future__ import annotations

import time
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-not-found]

from .keys import (
    idempotency_pk,
    pipeline_checkpoint_gsi_pk,
    pipeline_apply_gsi_pk,
    pipeline_apply_gsi_sk,
    run_meta_sk,
    run_pk,
    terminal_rank,
)

from . import _shared
from ._shared import (
    _TERMINAL_RANK,
    IdempotencyConflictError,
    RunRegistryError,
    _normalize,
    expire_ttl,
    is_expired,
)
from .step_index import validate_registry_step_count, validate_registry_step_range


def get_idempotency(trigger_id: str, idempotency_key: str) -> dict[str, Any] | None:
    item = _normalize(
        _shared._table()
        .get_item(
            Key={"pk": idempotency_pk(trigger_id), "sk": idempotency_key},
            ConsistentRead=True,
        )
        .get("Item")
    )
    if not item or is_expired(item):
        return None
    return item


def claim_idempotent_run(
    trigger_id: str,
    idempotency_key: str,
    *,
    request_fingerprint: str,
    run_record: dict[str, Any],
) -> tuple[str, bool]:
    """Atomically claim idempotency and create the initial run record."""
    existing = get_idempotency(trigger_id, idempotency_key)
    if existing:
        stored_fp = existing.get("request_fingerprint")
        if stored_fp != request_fingerprint:
            raise IdempotencyConflictError(
                "idempotency key reused with different request payload"
            )
        run_id = existing.get("run_id")
        if isinstance(run_id, str):
            return run_id, False
        raise RunRegistryError("idempotency record conflict without run_id")
    table = _shared._table()
    run_id = run_record["run_id"]
    ttl = expire_ttl(run_record.get("created_at"))
    now = int(time.time())
    idem_item = {
        "pk": idempotency_pk(trigger_id),
        "sk": idempotency_key,
        "run_id": run_id,
        "request_fingerprint": request_fingerprint,
        "expire_ttl": ttl,
        "created_at": run_record.get("created_at", now),
    }
    try:
        _shared.transact_write_items(
            transact_items=[
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": idem_item,
                        "ConditionExpression": "attribute_not_exists(pk) OR expire_ttl <= :now",
                        "ExpressionAttributeValues": {":now": now},
                    }
                },
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": run_record,
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ],
        )
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
            existing = get_idempotency(trigger_id, idempotency_key)
            if existing:
                if existing.get("request_fingerprint") != request_fingerprint:
                    raise IdempotencyConflictError(
                        "idempotency key reused with different request payload"
                    ) from error
                run_id = existing.get("run_id")
                if isinstance(run_id, str):
                    return run_id, False
        raise
    return run_id, True


def attach_sfn_execution_arn(run_id: str, sfn_execution_arn: str) -> None:
    """Attach the Step Functions ARN without changing terminal status."""
    now = int(time.time())
    _shared._table().update_item(
        Key={"pk": run_pk(run_id), "sk": run_meta_sk()},
        UpdateExpression="SET sfn_execution_arn = :arn, updated_at = :updated",
        ConditionExpression="attribute_exists(pk) AND attribute_not_exists(sfn_execution_arn)",
        ExpressionAttributeValues={":arn": sfn_execution_arn, ":updated": now},
    )


def set_run_deadline(run_id: str, deadline_at: str) -> None:
    """Persist the once-computed deadline without permitting later extension."""
    now = int(time.time())
    _shared._table().update_item(
        Key={"pk": run_pk(run_id), "sk": run_meta_sk()},
        UpdateExpression="SET deadline_at = :deadline, updated_at = :updated",
        ConditionExpression=(
            "attribute_exists(pk) AND "
            "(attribute_not_exists(deadline_at) OR deadline_at = :deadline)"
        ),
        ExpressionAttributeValues={":deadline": deadline_at, ":updated": now},
    )


def set_run_pipeline_metadata(run_id: str, *, pipeline: str, step_count: int) -> None:
    """Persist resolved pipeline metadata from the pinned checkout resolution."""
    if not pipeline:
        raise ValueError("pipeline must be a non-empty string")
    api_step_count = validate_registry_step_count(step_count)
    now = int(time.time())
    _shared._table().update_item(
        Key={"pk": run_pk(run_id), "sk": run_meta_sk()},
        UpdateExpression="SET pipeline = :pipeline, step_count = :step_count, updated_at = :updated",
        ConditionExpression=(
            "attribute_exists(pk) AND "
            "(attribute_not_exists(pipeline) OR pipeline = :pipeline) AND "
            "(attribute_not_exists(step_count) OR step_count = :step_count)"
        ),
        ExpressionAttributeValues={
            ":pipeline": pipeline,
            ":step_count": api_step_count,
            ":updated": now,
        },
    )


def mark_pipeline_checkpoint_succeeded(
    run_id: str,
    *,
    trigger_id: str,
    repo_name: str,
    pipeline: str,
    action: str,
    step_index: int,
    step_count: int,
    pipeline_sha256: str,
    pr_number: int,
    commit_hash: str,
    completed_at: int | None = None,
) -> None:
    """Index one successful pipeline mutation checkpoint for later progression checks."""
    if action not in {"apply", "destroy"}:
        raise ValueError("pipeline checkpoint action must be apply or destroy")
    if not run_id or not trigger_id or not repo_name or not pipeline:
        raise ValueError("pipeline checkpoint success identity fields are required")
    api_step_index, api_step_count = validate_registry_step_range(step_index, step_count)
    if not isinstance(pipeline_sha256, str) or not pipeline_sha256:
        raise ValueError("pipeline_sha256 must be a non-empty string")
    if type(pr_number) is not int or pr_number < 1:
        raise ValueError("pr_number must be a positive integer")
    if not isinstance(commit_hash, str) or len(commit_hash) != 40:
        raise ValueError("commit_hash must be a 40-character SHA")
    completed = int(time.time()) if completed_at is None else completed_at
    gsi_pk = pipeline_checkpoint_gsi_pk(
        trigger_id=trigger_id,
        repo_name=repo_name,
        pipeline=pipeline,
        action=action,
        step_index=api_step_index,
        pr_number=pr_number,
        commit_hash=commit_hash,
        pipeline_sha256=pipeline_sha256,
    )
    gsi_sk = pipeline_apply_gsi_sk(completed, run_id)
    _shared._table().update_item(
        Key={"pk": run_pk(run_id), "sk": run_meta_sk()},
        UpdateExpression=(
            "SET pipeline = :pipeline, step_index = :step_index, "
            "step_count = :step_count, pipeline_sha256 = :pipeline_sha256, "
            "pipeline_checkpoint_completed_at = :completed_at, "
            "gsi2pk = :gsi2pk, gsi2sk = :gsi2sk, updated_at = :updated"
        ),
        ConditionExpression=(
            "attribute_exists(pk) AND trigger_id = :trigger_id AND repo_name = :repo_name "
            "AND #action = :action AND #status = :succeeded AND "
            "(attribute_not_exists(pipeline) OR pipeline = :pipeline) AND "
            "(attribute_not_exists(step_index) OR step_index = :step_index) AND "
            "(attribute_not_exists(step_count) OR step_count = :step_count) AND "
            "(attribute_not_exists(pipeline_sha256) OR pipeline_sha256 = :pipeline_sha256)"
        ),
        ExpressionAttributeNames={"#action": "action", "#status": "status"},
        ExpressionAttributeValues={
            ":pipeline": pipeline,
            ":step_index": api_step_index,
            ":step_count": api_step_count,
            ":pipeline_sha256": pipeline_sha256,
            ":completed_at": completed,
            ":gsi2pk": gsi_pk,
            ":gsi2sk": gsi_sk,
            ":updated": completed,
            ":trigger_id": trigger_id,
            ":repo_name": repo_name,
            ":action": action,
            ":succeeded": "succeeded",
        },
    )


def mark_pipeline_apply_succeeded(
    run_id: str,
    *,
    trigger_id: str,
    repo_name: str,
    pipeline: str,
    step_index: int,
    step_count: int,
    pipeline_sha256: str,
    pr_number: int,
    commit_hash: str,
    completed_at: int | None = None,
) -> None:
    """Index one successful pipeline apply checkpoint for later step-order checks."""
    mark_pipeline_checkpoint_succeeded(
        run_id,
        trigger_id=trigger_id,
        repo_name=repo_name,
        pipeline=pipeline,
        action="apply",
        step_index=step_index,
        step_count=step_count,
        pipeline_sha256=pipeline_sha256,
        pr_number=pr_number,
        commit_hash=commit_hash,
        completed_at=completed_at,
    )


def update_run_status(
    run_id: str,
    status: str,
    *,
    sfn_execution_arn: str | None = None,
    drift_detected: bool | None = None,
) -> None:
    if drift_detected is not None and type(drift_detected) is not bool:
        raise ValueError("drift_detected must be a boolean")

    now = int(time.time())
    rank = terminal_rank(status)
    values: dict[str, Any] = {
        ":status": status,
        ":updated": now,
        ":rank": rank,
        ":terminal": _TERMINAL_RANK,
    }
    expression = "SET #status = :status, updated_at = :updated, status_rank = :rank"
    names = {"#status": "status"}
    if sfn_execution_arn:
        expression += ", sfn_execution_arn = :arn"
        values[":arn"] = sfn_execution_arn
    if drift_detected is not None:
        expression += ", drift_detected = :drift_detected"
        values[":drift_detected"] = drift_detected
    condition = (
        "attribute_exists(pk) AND ("
        "attribute_not_exists(status_rank) OR "
        "(status_rank < :terminal AND :rank >= status_rank) OR "
        "(:rank = status_rank AND #status = :status)"
        ")"
    )
    _shared._table().update_item(
        Key={"pk": run_pk(run_id), "sk": run_meta_sk()},
        UpdateExpression=expression,
        ConditionExpression=condition,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def get_run(run_id: str) -> dict[str, Any] | None:
    item = _normalize(
        _shared._table()
        .get_item(
            Key={"pk": run_pk(run_id), "sk": run_meta_sk()},
            ConsistentRead=True,
        )
        .get("Item")
    )
    if not item or is_expired(item):
        return None
    return item


def finalize_run_if_running(run_id: str, status: str) -> None:
    try:
        update_run_status(run_id, status)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
