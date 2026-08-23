# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry finalizer for outer Step Functions failures."""

from __future__ import annotations

import os
import re
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-not-found]

from src.core.logging import get_logger
from src.domain.locks import run_lock
from src.domain.run.outcome import normalize_map_outcome
from src.platform.aws.dynamo_resource import dynamo_table
from src.platform.aws.run_registry import (
    finalize_run_if_running,
    get_folder_attempt,
    put_folder_record,
)
from src.platform.aws.run_registry.step_index import registry_step_index_from_state

logger = get_logger(__name__)

_MAX_FINALIZE_ATTEMPTS = 3
_RUN_ID = re.compile(
    r"^(?:[0-9a-f]{32}|\d{1,20}\.[0-9a-f]{8}|[A-Za-z0-9_.-]{1,80})$",
    re.IGNORECASE,
)


def _event_run_id(event: dict[str, Any]) -> str:
    run_id = event.get("run_id")
    if isinstance(run_id, str) and _RUN_ID.fullmatch(run_id):
        return run_id
    detail = event.get("detail")
    if not isinstance(detail, dict):
        return ""
    execution_arn = detail.get("executionArn")
    if not isinstance(execution_arn, str):
        return ""
    candidate = execution_arn.rsplit(":", 1)[-1]
    return candidate if _RUN_ID.fullmatch(candidate) else ""


def _release_locks(event: dict[str, Any]) -> list[str]:
    """Release from durable run ownership, never from a transient ASL envelope."""
    locks_table = os.environ.get("LOCKS_TABLE_NAME")
    run_id = _event_run_id(event)
    if not locks_table or not run_id:
        return []
    table = dynamo_table(locks_table)
    for attempt_no in range(_MAX_FINALIZE_ATTEMPTS):
        try:
            run_lock.release_all(table, run_id)
            return []
        except (ClientError, BotoCoreError, OSError, ValueError) as error:
            if attempt_no + 1 >= _MAX_FINALIZE_ATTEMPTS:
                return [str(error)[:2048]]
            time.sleep(0.2 * (attempt_no + 1))
    return ["durable lock release failed"]


def _persist_one_folder(event: dict[str, Any], item: dict[str, Any]) -> str | None:
    run_id = _event_run_id(event)
    if not run_id or not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return None
    normalized = normalize_map_outcome(item)
    folder = normalized.get("folder")
    if not isinstance(folder, str):
        return "missing folder"
    execution_id = normalized.get("execution_id") or normalized.get("exec_id")
    if folder == "config" and (not isinstance(execution_id, str) or not execution_id):
        execution_id = f"config-{run_id}"
    if not isinstance(execution_id, str) or not execution_id:
        return f"{folder}: missing execution id"
    attempt = int(normalized.get("attempt") or 0)
    try:
        existing = get_folder_attempt(run_id, folder, attempt)
    except (ClientError, BotoCoreError, OSError, ValueError, TypeError) as error:
        return f"{folder}: failed to read persisted attempt: {error}"[:2048]
    if existing is not None:
        if existing.get("execution_id") != execution_id:
            return f"{folder}: persisted attempt execution id mismatch"
        return None
    for attempt_no in range(_MAX_FINALIZE_ATTEMPTS):
        try:
            put_folder_record(
                run_id=run_id,
                folder=folder,
                account_id=str(
                    normalized.get("account_id") or item.get("account_id") or ""
                ),
                execution_id=execution_id,
                attempt=attempt,
                status=str(
                    normalized.get("status")
                    or ("succeeded" if normalized.get("succeeded") else "failed")
                ),
                manifest_s3_uri=normalized.get("manifest_s3_uri")
                if isinstance(normalized.get("manifest_s3_uri"), str)
                else None,
                manifest_sha256=normalized.get("manifest_sha256")
                if isinstance(normalized.get("manifest_sha256"), str)
                else None,
                outcome=normalized.get("outcome")
                if isinstance(normalized.get("outcome"), dict)
                else normalized,
                deadline_at=str(event["deadline_at"])
                if isinstance(event.get("deadline_at"), str)
                else None,
                drift_detected=normalized.get("drift_detected")
                if type(normalized.get("drift_detected")) is bool
                else None,
                step_index=registry_step_index_from_state(normalized.get("step_index")),
            )
            return None
        except RuntimeError as error:
            return f"{folder}: {error}"[:2048]
        except (ClientError, BotoCoreError, OSError, ValueError, TypeError) as error:
            if attempt_no + 1 >= _MAX_FINALIZE_ATTEMPTS:
                return f"{folder}: {error}"[:2048]
            time.sleep(0.2 * (attempt_no + 1))
    return f"{folder}: persistence failed"


def _persist_folder_outcomes(event: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen_folders: set[str] = set()
    for item in event.get("outcomes") or []:
        if not isinstance(item, dict):
            continue
        normalized = normalize_map_outcome(item)
        folder = normalized.get("folder")
        if isinstance(folder, str):
            seen_folders.add(folder)
        error = _persist_one_folder(event, item)
        if error:
            errors.append(error)
    for item in event.get("map_items") or []:
        if not isinstance(item, dict):
            continue
        folder = item.get("folder")
        if not isinstance(folder, str) or folder in seen_folders:
            continue
        synthesized = {
            "folder": folder,
            "account_id": item.get("account_id") or "",
            "execution_id": item.get("execution_id")
            or item.get("exec_id")
            or f"missing-{folder}",
            "attempt": item.get("attempt") or 0,
            "status": "failed",
            "succeeded": False,
            "outcome": {"succeeded": False, "error": "missing map outcome"},
            "step_index": item.get("step_index"),
        }
        error = _persist_one_folder(event, synthesized)
        if error:
            errors.append(error)
    return errors


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    run_id = _event_run_id(event)
    logger.info("finalize_run handler invoked", extra={"run_id": run_id})
    registry_error: str | None = None
    folder_errors = _persist_folder_outcomes(event)
    lock_errors = _release_locks(event)
    if run_id and os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        for attempt in range(_MAX_FINALIZE_ATTEMPTS):
            try:
                finalize_run_if_running(run_id, "failed")
                registry_error = None
                break
            except (ClientError, BotoCoreError, OSError) as error:
                registry_error = str(error)[:2048]
                if attempt + 1 < _MAX_FINALIZE_ATTEMPTS:
                    time.sleep(0.2 * (attempt + 1))
    finalized = registry_error is None and not folder_errors and not lock_errors
    if not finalized:
        message = "finalization incomplete"
        if folder_errors:
            message = folder_errors[0]
        elif lock_errors:
            message = lock_errors[0]
        elif registry_error:
            message = registry_error
        raise RuntimeError(message)
    logger.info("finalize_run handler completed", extra={"run_id": run_id})
    return {"finalized": True}
