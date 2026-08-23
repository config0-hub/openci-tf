"""Adapt verified GitHub webhook events into transport-neutral run requests."""
from __future__ import annotations

from typing import Any

from src.domain.run.request import (
    NotificationTarget,
    RunRequest,
    RunRequestValidationError,
    build_run_request,
)


def github_run_request(
    info: dict[str, Any],
    *,
    action: str,
    folders: list[str],
    all_flag: bool,
    affected_flag: bool,
    delivery_id: str,
    confirm_token: str | None = None,
    intent_create: bool = False,
    intent_confirm: bool = False,
    pipeline: str | None = None,
    pipeline_step: int | None = None,
) -> RunRequest:
    if not isinstance(delivery_id, str) or not delivery_id.strip():
        raise RunRequestValidationError("github delivery id is required")
    trigger_id = str(info.get("trigger_id") or "")
    commit_hash = str(info.get("commit_hash") or "")
    if not trigger_id or not commit_hash:
        raise RunRequestValidationError("github ingress missing trigger_id or commit_hash")
    if pipeline is not None:
        folder_mode = "pipeline"
        selected: list[str] = []
    elif all_flag:
        folder_mode = "all"
        selected = []
    elif affected_flag:
        folder_mode = "affected"
        selected = []
    elif folders:
        folder_mode = "explicit"
        selected = folders
    else:
        folder_mode = "affected"
        selected = []
    pr_number = info.get("pr_number")
    notification = NotificationTarget("github_pr", int(pr_number)) if isinstance(pr_number, int) else NotificationTarget("registry")
    request = build_run_request(
        trigger_id=trigger_id,
        commit_hash=commit_hash,
        action=action,
        folder_mode=folder_mode,
        folders=selected,
        idempotency_key=delivery_id.strip(),
        notification_target=notification,
        ingress_source="github",
        pipeline=pipeline,
        pipeline_step=pipeline_step,
    )
    request.github_metadata = {  # type: ignore[attr-defined]
        key: value
        for key, value in {
            "delivery_id": delivery_id.strip(),
            "comment_id": info.get("comment_id"),
            "event_type": info.get("event_type"),
            "username": info.get("username"),
            "confirm_token": confirm_token,
            "intent_create": intent_create,
            "intent_confirm": intent_confirm,
            "pipeline_step": pipeline_step,
        }.items()
        if value is not None
    }
    return request
