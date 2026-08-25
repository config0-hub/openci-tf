# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Intent create/confirm Lambda handlers for the outer state machine."""
from __future__ import annotations

import os
from typing import Any

from src.core.logging import get_logger
from src.domain.formatters.intent import intent_failure_comment, intent_success_comment
from src.domain.intent.models import IntentGateFailure, IntentRecord
from src.platform.aws.run_registry import set_run_pipeline_metadata
from src.platform.aws.ssm import get_github_token
from src.platform.github.client import GitHubClient
from src.services.intent.confirm import confirm_intent
from src.services.intent.create import IntentCreationError, create_intent
from src.services.webhook.pr_comment import post_pr_comment

logger = get_logger(__name__)


def _post_comment(webhook_info: dict[str, Any], settings: dict[str, Any], body: str) -> None:
    post_pr_comment(webhook_info, settings, body)


def _current_pr_head_sha(settings: dict[str, Any], repo: str, pr_number: int) -> str:
    token = get_github_token(settings["ssm_openci_tf_github_token"])
    return GitHubClient(token).get_pr_head_sha(repo, pr_number)


def _record_confirmed_pipeline_metadata(event: dict[str, Any], confirmed: dict[str, Any]) -> None:
    pipeline = confirmed.get("pipeline")
    if pipeline is None:
        return
    if not isinstance(pipeline, str) or not pipeline:
        raise ValueError("confirmed pipeline must be a non-empty string")
    step_count = confirmed.get("step_count")
    if type(step_count) is not int:
        raise ValueError("confirmed pipeline step_count must be an integer")
    if not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("confirmed pipeline run requires run_id")
    set_run_pipeline_metadata(run_id, pipeline=pipeline, step_count=step_count)


def create_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    action = str(event.get("action") or "")
    folders = event.get("folders") or []
    webhook = event["webhook_info"]
    settings = event["settings"]
    pr_number = webhook.get("pr_number")
    trigger_id = webhook.get("trigger_id")
    repo_name = webhook.get("repo_name")
    if (
        not isinstance(pr_number, int)
        or not isinstance(trigger_id, str)
        or not isinstance(repo_name, str)
    ):
        raise TypeError("intent creation requires pr_number, trigger_id, and repo_name")
    commit_hash = _current_pr_head_sha(settings, repo_name, pr_number)
    if not isinstance(folders, list) or not all(isinstance(folder, str) for folder in folders):
        raise ValueError("folders must be a list of strings")
    try:
        failure, record = create_intent(
            action=action,
            folders=folders,
            trigger_id=trigger_id,
            pr_number=pr_number,
            commit_hash=commit_hash,
            pipeline=event.get("pipeline") if isinstance(event.get("pipeline"), str) else None,
            pipeline_step=event.get("pipeline_step") if isinstance(event.get("pipeline_step"), int) else None,
        )
    except IntentCreationError as error:
        failures = [IntentGateFailure(str(error))]
        body = intent_failure_comment(action, failures)
        _post_comment(webhook, settings, body)
        return {**event, "intent_failed": True, "intent_failures": [failure.message for failure in failures]}
    if failure is not None or record is None:
        failures = [failure] if failure is not None else [IntentGateFailure("intent gate failed")]
        body = intent_failure_comment(action, failures)
        _post_comment(webhook, settings, body)
        return {**event, "intent_failed": True, "intent_failures": [item.message for item in failures]}
    summaries = [f"- `{folder}`: pinned plan from execution `{record['source_run_id']}`" for folder in record["folders"]]
    body = intent_success_comment(
        IntentRecord(
            token=record["token"],
            trigger_id=record["trigger_id"],
            pr_number=record["pr_number"],
            action=record["action"],
            source_run_id=record["source_run_id"],
            folders=tuple(record["folders"]),
            commit_hash=record["commit_hash"],
            folder_pins=(),
            expires_at=record["expires_at"],
            pipeline=record.get("pipeline") if isinstance(record.get("pipeline"), str) else None,
            step_index=record.get("step_index") if isinstance(record.get("step_index"), int) else None,
            step_count=record.get("step_count") if isinstance(record.get("step_count"), int) else None,
            pipeline_sha256=record.get("pipeline_sha256") if isinstance(record.get("pipeline_sha256"), str) else None,
        ),
        plan_summaries=summaries,
    )
    _post_comment(webhook, settings, body)
    return {**event, "intent_created": True, "intent_token": record["token"]}


def confirm_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    action = str(event.get("action") or "")
    token = str(event.get("confirm_token") or "")
    webhook = event["webhook_info"]
    settings = event["settings"]
    pr_number = webhook.get("pr_number")
    trigger_id = webhook.get("trigger_id")
    repo_name = webhook.get("repo_name")
    if not isinstance(pr_number, int) or not isinstance(trigger_id, str) or not isinstance(repo_name, str):
        raise TypeError("confirm requires pr_number, trigger_id, and repo_name")
    commit_hash = _current_pr_head_sha(settings, repo_name, pr_number)
    failures, confirmed = confirm_intent(
        token=token,
        action=action,
        commit_hash=commit_hash,
        trigger_id=trigger_id,
        pr_number=pr_number,
        repo_name=repo_name,
    )
    if failures or confirmed is None:
        body = intent_failure_comment(action, failures or [IntentGateFailure("confirmation failed")])
        _post_comment(webhook, settings, body)
        return {**event, "intent_failed": True, "intent_failures": [item.message for item in failures]}
    _record_confirmed_pipeline_metadata(event, confirmed)
    webhook_updates: dict[str, Any] = {"commit_hash": commit_hash}
    if isinstance(confirmed.get("pipeline"), str):
        webhook_updates["pipeline"] = confirmed["pipeline"]
    if isinstance(confirmed.get("step_index"), int):
        webhook_updates["pipeline_step_index"] = confirmed["step_index"]
    if isinstance(confirmed.get("step_count"), int):
        webhook_updates["pipeline_step_count"] = confirmed["step_count"]
    if isinstance(confirmed.get("pipeline_sha256"), str):
        webhook_updates["pipeline_sha256"] = confirmed["pipeline_sha256"]
    return {
        **event,
        "action": confirmed["action"],
        "folders": confirmed["folders"],
        # Confirmation must execute exactly the folder set stored in the intent.
        # The confirm command itself has no folders, so parse-time selection flags
        # describe the PR diff rather than the pinned intent and must not win.
        "all_flag": False,
        "affected_flag": False,
        "folder_pins": confirmed["folder_pins"],
        "source_plan_run_id": confirmed["source_plan_run_id"],
        "commit_hash": commit_hash,
        "webhook_info": {**webhook, **webhook_updates},
        "confirm_token": None,
        "intent_confirmed": True,
    }
