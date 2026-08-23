# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run-registry terminal-status persistence for render."""

from __future__ import annotations

import os
from typing import Any

from src.domain.engine.invocation_id import derive_run_id
from src.platform.aws.run_registry.step_index import registry_step_index_from_state


def _resolve_run_id(event: dict[str, Any]) -> str:
    run_id = event.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    webhook = event.get("webhook_info")
    if isinstance(webhook, dict):
        return derive_run_id(webhook)
    raise ValueError("run_id is required for registry persistence")


def _terminal_status(
    outcomes: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> str:
    if not outcomes and skipped:
        return "skipped"
    terminal = "succeeded"
    for item in outcomes + skipped:
        status = str(item.get("status") or "")
        if (
            status in {"failed", "infrastructure_error"}
            or item.get("succeeded") is False
        ):
            return "failed"
        if status == "in_progress":
            terminal = "skipped"
    return terminal


def _run_drift_detected(
    outcomes: list[dict[str, Any]],
    action: str,
) -> bool | None:
    if action != "drift":
        return None
    has_unknown = False
    for outcome in outcomes:
        value = outcome.get("drift_detected")
        if value is True:
            return True
        if value is not False:
            has_unknown = True
    if outcomes and not has_unknown:
        return False
    return None


def _successful_pipeline_apply_metadata(event: dict[str, Any]) -> dict[str, Any] | None:
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return None
    pipeline = webhook.get("pipeline")
    if pipeline is None:
        return None
    step_index = webhook.get("pipeline_step_index")
    step_count = webhook.get("pipeline_step_count")
    pipeline_sha256 = webhook.get("pipeline_sha256")
    trigger_id = webhook.get("trigger_id")
    repo_name = webhook.get("repo_name")
    if not isinstance(pipeline, str) or not pipeline:
        raise ValueError("pipeline must be a non-empty string")
    if type(step_index) is not int or step_index < 1:
        raise ValueError("pipeline_step_index must be an integer >= 1")
    if type(step_count) is not int or step_count < 1 or step_index > step_count:
        raise ValueError("pipeline_step_count must be an integer >= pipeline_step_index")
    if not isinstance(pipeline_sha256, str) or not pipeline_sha256:
        raise ValueError("pipeline_sha256 must be a non-empty string")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise ValueError("trigger_id is required for pipeline apply registry metadata")
    if not isinstance(repo_name, str) or not repo_name:
        raise ValueError("repo_name is required for pipeline apply registry metadata")
    return {
        "trigger_id": trigger_id,
        "repo_name": repo_name,
        "pipeline": pipeline,
        "step_index": step_index,
        "step_count": step_count,
        "pipeline_sha256": pipeline_sha256,
    }


def _update_run_registry(
    event: dict[str, Any],
    outcomes: list[dict[str, Any]],
    action: str,
    *,
    skipped: list[dict[str, Any]] | None = None,
) -> None:
    if not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return
    from src.platform.aws.run_registry import (
        mark_pipeline_apply_succeeded,
        put_folder_record,
        update_run_status,
    )

    run_id = _resolve_run_id(event)
    skipped_items = skipped if skipped is not None else list(event.get("skipped", []))
    terminal = _terminal_status(outcomes, skipped_items)
    for outcome in outcomes + skipped_items:
        status = str(
            outcome.get("status")
            or ("succeeded" if outcome.get("succeeded") else "failed")
        )
        folder = str(outcome.get("folder") or "")
        if not folder or folder == "config":
            if folder == "config":
                put_folder_record(
                    run_id=run_id,
                    folder="config",
                    account_id=str(outcome.get("account_id") or ""),
                    execution_id=str(
                        outcome.get("execution_id")
                        or outcome.get("exec_id")
                        or f"config-{run_id}"
                    ),
                    attempt=int(outcome.get("attempt") or 0),
                    status=status,
                    manifest_s3_uri=outcome.get("manifest_s3_uri")
                    if isinstance(outcome.get("manifest_s3_uri"), str)
                    else None,
                    outcome=outcome,
                    deadline_at=str(event["deadline_at"])
                    if isinstance(event.get("deadline_at"), str)
                    else None,
                    step_index=registry_step_index_from_state(outcome.get("step_index")),
                )
            continue
        execution_id = outcome.get("execution_id") or outcome.get("exec_id")
        if not isinstance(execution_id, str):
            execution_id = f"skipped-{folder}"
        raw_pointers = outcome.get("pointers")
        pointers: dict[str, Any] = (
            raw_pointers if isinstance(raw_pointers, dict) else {}
        )
        manifest_uri = outcome.get("manifest_s3_uri")
        if not isinstance(manifest_uri, str):
            pointer_manifest = pointers.get("manifest")
            manifest_uri = (
                pointer_manifest if isinstance(pointer_manifest, str) else None
            )
        manifest_digest = outcome.get("manifest_sha256")
        if not isinstance(manifest_digest, str):
            manifest_digest = None
        put_folder_record(
            run_id=run_id,
            folder=folder,
            account_id=str(outcome.get("account_id") or ""),
            execution_id=execution_id,
            attempt=int(outcome.get("attempt") or 0),
            status=status,
            manifest_s3_uri=manifest_uri if isinstance(manifest_uri, str) else None,
            manifest_sha256=manifest_digest,
            outcome=outcome,
            deadline_at=str(event["deadline_at"])
            if isinstance(event.get("deadline_at"), str)
            else None,
            drift_detected=outcome.get("drift_detected")
            if type(outcome.get("drift_detected")) is bool
            else None,
            step_index=registry_step_index_from_state(outcome.get("step_index")),
        )
    update_run_status(
        run_id,
        terminal,
        drift_detected=_run_drift_detected(outcomes + skipped_items, action),
    )
    if action == "apply" and terminal == "succeeded":
        metadata = _successful_pipeline_apply_metadata(event)
        if metadata is not None:
            mark_pipeline_apply_succeeded(run_id, **metadata)
