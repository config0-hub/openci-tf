# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render bounded execution outcomes into deterministic PR comments."""

from __future__ import annotations

import os
import time
from typing import Any, cast

import boto3  # type: ignore[import-not-found]
import requests

from src.core.logging import get_logger
from src.core.models import DEFAULT_APPLY_GRACE_SECONDS, DEFAULT_DESTROY_GRACE_SECONDS
from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_limits import (
    ALLOWED_ARTIFACT_CONTENT_TYPES,
    MAX_ARTIFACT_BYTES,
)
from src.domain.formatters.artifacts import (
    folder_comment,
    mutation_status_comment_in_progress,
    mutation_terminal_comment,
    pending_plan_comment,
    pending_summary,
    pipeline_plan_preview_comment,
    pipeline_mutation_aggregate_comment,
    status_comment_in_progress,
    summary,
    _pipeline_mutation_result_label,
)
from src.domain.formatters.console_urls import step_functions_execution_url
from src.domain.intent.models import intent_record_matches_current_request
from src.domain.locks import run_lock
from src.domain.run.outcome import normalize_map_outcome as _outcome
from src.domain.engine.outer_map_state import _items_for_step
from src.domain.engine.summary import (
    build_outer_map_outcome,
    validate_outer_map_outcome,
)
from src.platform.aws.intent_registry import get_intent_record
from src.domain.engine.artifact_paths import pr_pointer_key
from src.platform.aws.s3 import get_bounded_json, list_text_prefix
from src.platform.aws.ssm import get_github_token
from src.platform.github.client import GitHubClient, comment_url
from src.platform.github.command_comment_cleanup import (
    delete_acknowledged_command_comment,
    delete_acknowledged_command_comments,
    delete_stale_confirm_token_comments,
    defer_command_comment_cleanup,
)
from src.domain.github.comment_object_id import (
    body_is_confirm_intent_comment,
    should_emit_comment_object_marker,
)
from src.services.render.artifact_access import (
    _artifact_list_prefix,
    _fetch_source_plan_text,
    _plan_artifact_metadata,
    _publish_report_all_pointer,
    _s3_bucket_key,
    _scoped_pr_context,
)
from src.services.render.comments import (
    _delete_and_repost,
    _delete_and_repost_unmanaged,
    _delete_generated_comment,
    _delete_transient_status_comment,
    _managed_comment_marker as _comments_managed_comment_marker,
    _upsert_managed_comment,
    _with_cleanup_warnings,
    _with_command_context,
)
from src.services.render.registry_update import (
    _resolve_run_id,
    _run_drift_detected as _registry_run_drift_detected,
    _terminal_status,
    _update_run_registry,
)

_FAILED_OUTCOME_STATUSES = frozenset({"failed", "infrastructure_error"})
_TERMINAL_CLEANUP_ATTEMPTS = 3
_TERMINAL_CLEANUP_RETRY_SECONDS = 0.25

logger = get_logger(__name__)

# Re-exported for existing unit tests that pin the render/registry seam.
_managed_comment_marker = _comments_managed_comment_marker
_run_drift_detected = _registry_run_drift_detected


def _summary_uses_report_all(action: str) -> bool:
    return action == "report"


def _aws_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )


def _console_url(execution_arn: str | None) -> str | None:
    if not isinstance(execution_arn, str) or not execution_arn:
        return None
    return step_functions_execution_url(execution_arn, region=_aws_region())


def _should_list_execution_artifacts(outcome: dict[str, Any]) -> bool:
    status = str(outcome.get("status") or "")
    if status in {"infrastructure_error", "in_progress", "skipped"}:
        return False
    execution_id = outcome.get("execution_id") or outcome.get("exec_id")
    return isinstance(execution_id, str) and bool(execution_id)


def _mutation_grace_seconds(item: dict[str, Any], action: str) -> int:
    raw = item.get("grace_seconds")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if action == "apply":
        return DEFAULT_APPLY_GRACE_SECONDS
    if action == "destroy":
        return DEFAULT_DESTROY_GRACE_SECONDS
    raise ValueError(f"missing grace_seconds for mutation action {action}")


def _mutation_codebuild_url(outcome: dict[str, Any]) -> str | None:
    build_id = outcome.get("codebuild_build_id")
    project = os.environ.get("ENGINE_CODEBUILD_PROJECT_NAME", "")
    if not project:
        return None
    from src.core.aws_ids import is_valid_codebuild_build_id
    from src.domain.formatters.console_urls import codebuild_build_url

    if not isinstance(build_id, str) or not is_valid_codebuild_build_id(build_id):
        exec_id = outcome.get("execution_id") or outcome.get("exec_id")
        if not isinstance(exec_id, str):
            return None
        from src.platform.aws import engine

        build_id = engine.resolve_codebuild_build_id(
            project, exec_id, max_attempts=1, sleep_seconds=0
        )
    if not isinstance(build_id, str) or not is_valid_codebuild_build_id(build_id):
        return None
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    return codebuild_build_url(
        project,
        build_id,
        region=region,
        account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        identity_center_start_url=os.environ.get("AWS_CONSOLE_START_URL") or None,
        identity_center_role_name=os.environ.get("AWS_CONSOLE_ROLE_NAME") or None,
    )


def _source_plan_run_id(outcome: dict[str, Any]) -> str | None:
    raw = outcome.get("source_plan_run_id")
    if isinstance(raw, str) and raw:
        return raw
    manifest_uri = outcome.get("manifest_s3_uri")
    if not isinstance(manifest_uri, str) or not manifest_uri.startswith("s3://"):
        pointers = outcome.get("pointers")
        if isinstance(pointers, dict):
            manifest_uri = pointers.get("manifest")
    if not isinstance(manifest_uri, str) or not manifest_uri.startswith("s3://"):
        return None
    bucket, key = _s3_bucket_key(manifest_uri)
    manifest = get_bounded_json(bucket, key, 65_536)
    if not isinstance(manifest, dict):
        return None
    source = manifest.get("source_plan_run_id")
    return source if isinstance(source, str) and source else None


def _pipeline_mutation_footer(
    event: dict[str, Any],
    action: str,
    outcomes: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> str | None:
    if action not in {"apply", "destroy"}:
        return None
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return None
    pipeline = webhook.get("pipeline")
    step_index = webhook.get("pipeline_step_index")
    step_count = webhook.get("pipeline_step_count")
    if not isinstance(pipeline, str) or not pipeline:
        return None
    if not isinstance(step_index, int) or isinstance(step_index, bool):
        return None
    if not isinstance(step_count, int) or isinstance(step_count, bool):
        return None
    if step_index < 1 or step_count < 1 or step_index > step_count:
        raise ValueError("invalid pipeline mutation step metadata")
    if _terminal_status(outcomes, skipped) != "succeeded":
        return None
    if step_index < step_count:
        return (
            f"> [!NOTE]\n"
            f"> Next step: `tf {action} pipeline {pipeline} step {step_index + 1}`"
        )
    verb = "applied" if action == "apply" else "destroyed"
    return f"> [!NOTE]\n> Pipeline `{pipeline}` complete ({step_count} folders {verb})."


def _is_pipeline_mutation(event: dict[str, Any], action: str) -> bool:
    if action not in {"apply", "destroy", "plan", "plan_destroy"}:
        return False
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return False
    pipeline = webhook.get("pipeline")
    if not isinstance(pipeline, str) or not pipeline:
        return False
    if action in {"plan", "plan_destroy"}:
        return event.get("pipeline_mutation_plan_first") is True
    return True


def _pipeline_mutation_action(event: dict[str, Any], action: str) -> str:
    pending = event.get("pending_mutation_action")
    if isinstance(pending, str) and pending in {"apply", "destroy"}:
        return pending
    return action


def _pipeline_aggregate_identity(event: dict[str, Any], action: str) -> dict[str, Any] | None:
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return None
    pipeline = webhook.get("pipeline")
    pipeline_sha256 = webhook.get("pipeline_sha256")
    trigger_id = webhook.get("trigger_id")
    repo_name = webhook.get("repo_name")
    pr_number = webhook.get("pr_number")
    commit_hash = webhook.get("commit_hash")
    mutation_action = _pipeline_mutation_action(event, action)
    if mutation_action not in {"apply", "destroy"}:
        return None
    if (
        not isinstance(pipeline, str)
        or not isinstance(pipeline_sha256, str)
        or not isinstance(trigger_id, str)
        or not isinstance(repo_name, str)
        or type(pr_number) is not int
        or not isinstance(commit_hash, str)
    ):
        return None
    return {
        "trigger_id": trigger_id,
        "repo_name": repo_name,
        "pipeline": pipeline,
        "action": mutation_action,
        "pr_number": pr_number,
        "commit_hash": commit_hash.lower(),
        "pipeline_sha256": pipeline_sha256,
    }


def _checkpoint_row_from_outcome(
    *,
    step_index: int,
    step_count: int,
    action: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    plan_pending: bool,
) -> dict[str, Any]:
    folder = str(outcome.get("folder") or "")
    plan_show = artifacts.get("plan-show.out", "")
    pinned = "plan.tfplan" if action == "apply" else "destroy.plan.tfplan"
    if plan_pending:
        return {
            "checkpoint_index": step_index,
            "folder": folder,
            "account_id": str(outcome.get("account_id") or ""),
            "plan_show_text": plan_show or None,
            "pinned_plan_artifact": pinned,
            "replanned_after_prior": step_index > 1,
            "confirmation_status": "Confirmation required",
            "result_label": "Plan ready ⏳",
        }
    succeeded = outcome.get("succeeded") is True and str(outcome.get("status") or "") not in {
        "failed",
        "infrastructure_error",
        "in_progress",
        "skipped",
    }
    return {
        "checkpoint_index": step_index,
        "folder": folder,
        "account_id": str(outcome.get("account_id") or ""),
        "plan_show_text": plan_show or None,
        "pinned_plan_artifact": pinned,
        "replanned_after_prior": step_index > 1,
        "confirmation_status": "Confirmed ✅",
        "result_label": _pipeline_mutation_result_label(action, succeeded=succeeded),
        "succeeded": succeeded,
    }


def _merge_checkpoint_rows(
    prior_rows: list[dict[str, Any]],
    current_row: dict[str, Any],
) -> list[dict[str, Any]]:
    merged = [row for row in prior_rows if row.get("checkpoint_index") != current_row["checkpoint_index"]]
    merged.append(current_row)
    merged.sort(key=lambda row: int(row.get("checkpoint_index") or 0))
    return merged


def _pipeline_mutation_aggregate_body(
    event: dict[str, Any],
    *,
    action: str,
    outcomes: list[dict[str, Any]],
    artifacts_by_folder: dict[str, dict[str, str]],
    commit_hash: str,
    footer: str | None,
    plan_pending: bool = False,
) -> tuple[str, list[dict[str, Any]], int | None]:
    webhook = event["webhook_info"]
    pipeline = str(webhook.get("pipeline") or "")
    step_index = webhook.get("pipeline_step_index")
    step_count = webhook.get("pipeline_step_count")
    if not isinstance(step_index, int) or not isinstance(step_count, int):
        raise ValueError("pipeline mutation render requires step metadata")
    if not outcomes:
        raise ValueError("pipeline mutation render requires one folder outcome")
    mutation_action = _pipeline_mutation_action(event, action)
    outcome = outcomes[0]
    folder = str(outcome.get("folder") or "")
    artifacts = artifacts_by_folder.get(folder, {})
    current_row = _checkpoint_row_from_outcome(
        step_index=step_index,
        step_count=step_count,
        action=mutation_action,
        outcome=outcome,
        artifacts=artifacts,
        plan_pending=plan_pending,
    )
    prior_rows: list[dict[str, Any]] = []
    existing_comment_id: int | None = None
    cumulative_succeeded: int | None = None
    cumulative_failed: int | None = None
    identity = _pipeline_aggregate_identity(event, action)
    if identity is not None and os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        from src.platform.aws.run_registry.pipeline_aggregate import (
            get_pipeline_aggregate_state,
        )

        state = get_pipeline_aggregate_state(**identity)
        if isinstance(state, dict):
            if isinstance(state.get("checkpoint_rows"), list):
                prior_rows = [
                    row for row in state["checkpoint_rows"] if isinstance(row, dict)
                ]
            if type(state.get("comment_id")) is int:
                existing_comment_id = state["comment_id"]
            if type(state.get("cumulative_succeeded")) is int:
                cumulative_succeeded = state["cumulative_succeeded"]
            if type(state.get("cumulative_failed")) is int:
                cumulative_failed = state["cumulative_failed"]
    checkpoint_rows = _merge_checkpoint_rows(prior_rows, current_row)
    if cumulative_succeeded is not None or cumulative_failed is not None:
        prior_indexes = {
            int(row.get("checkpoint_index") or 0) for row in prior_rows
        }
        current_index = int(current_row.get("checkpoint_index") or 0)
        if current_index not in prior_indexes:
            if current_row.get("succeeded") is True:
                cumulative_succeeded = (cumulative_succeeded or 0) + 1
            elif current_row.get("succeeded") is False:
                cumulative_failed = (cumulative_failed or 0) + 1
    body = pipeline_mutation_aggregate_comment(
        action=mutation_action,
        pipeline=pipeline,
        checkpoint_count=step_count,
        checkpoint_rows=checkpoint_rows,
        footer=footer,
        metadata_lines=[
            f"- Run ID: `{event.get('run_id')}`",
            f"- Source plan run ID: `{_source_plan_run_id(outcome) or event.get('run_id') or 'unknown'}`",
        ],
        cumulative_succeeded=cumulative_succeeded,
        cumulative_failed=cumulative_failed,
    )
    return body, checkpoint_rows, existing_comment_id


def _pipeline_apply_footer(
    event: dict[str, Any],
    action: str,
    outcomes: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> str | None:
    return _pipeline_mutation_footer(event, action, outcomes, skipped)


def _append_footer(body: str, footer: str | None) -> str:
    if footer is None:
        return body
    return f"{body}\n\n{footer}"


def _mutation_folder_comment(
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    *,
    action: str,
    commit_hash: str,
    console_url: str | None,
    run_id: str,
    repo_name: str,
    pr_number: int | None,
    existing_names: frozenset[str] | None = None,
) -> str:
    status = str(outcome.get("status") or "")
    succeeded = outcome.get("succeeded") is True and status not in {
        "failed",
        "infrastructure_error",
        "in_progress",
        "skipped",
    }
    plan_artifact = "plan.tfplan" if action == "apply" else "destroy.plan.tfplan"
    source_run_id = _source_plan_run_id(outcome)
    plan_show = artifacts.get("plan-show.out", "")
    if not plan_show.strip() and succeeded and source_run_id:
        plan_show = _fetch_source_plan_text(
            repo_name=repo_name,
            folder=folder,
            action=action,
            source_run_id=source_run_id,
            pr_number=pr_number,
        ) or ""
    plan_pointer = None
    if plan_show and len(plan_show.encode("utf-8")) > 8000:
        plan_pointer = f"openci-tf/{run_id}/{folder}/plan-show.out"
    codebuild_url = _mutation_codebuild_url(outcome)
    terminal_error = None
    if not succeeded:
        terminal_error = redact_and_bound_terminal_evidence(
            outcome.get("error") or "unknown error"
        )
        if not isinstance(terminal_error, str):
            raise TypeError("mutation terminal error must be a string")
    return mutation_terminal_comment(
        action=action,
        folder=folder,
        account_id=str(outcome.get("account_id") or ""),
        commit_hash=commit_hash,
        succeeded=succeeded,
        pinned_plan_artifact=plan_artifact,
        console_url=console_url,
        codebuild_url=codebuild_url,
        codebuild_account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        plan_show_text=plan_show or None,
        plan_show_pointer=plan_pointer,
        source_plan_run_id=source_run_id,
        error=terminal_error,
        run_id=run_id,
        repo_name=repo_name,
        pr_number=pr_number,
        existing_names=existing_names,
        tmp_bucket=os.environ.get("TMP_BUCKET_NAME", ""),
        region=_aws_region(),
        hub_account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        identity_center_start_url=os.environ.get("AWS_CONSOLE_START_URL") or None,
        identity_center_role_name=os.environ.get("AWS_CONSOLE_ROLE_NAME") or None,
    )


def _render_folder_body(
    folder: str,
    outcome: dict[str, Any],
    artifacts: dict[str, str],
    *,
    action: str,
    commit_hash: str,
    console_url: str | None,
    run_id: str,
    repo: str,
    render_items: list[dict[str, Any]],
    pr_number: int | None = None,
    existing_names: frozenset[str] | None = None,
    approved_plan_pointer_key: str | None = None,
) -> str:
    if action in {"apply", "destroy"}:
        return _mutation_folder_comment(
            folder,
            outcome,
            artifacts,
            action=action,
            commit_hash=commit_hash,
            console_url=console_url,
            run_id=run_id,
            repo_name=repo,
            pr_number=pr_number,
            existing_names=existing_names,
        )
    return folder_comment(
        folder,
        outcome,
        artifacts,
        action=action,
        commit_hash=commit_hash,
        console_url=console_url,
        run_id=run_id,
        repo_name=repo,
        pr_number=pr_number,
        existing_names=existing_names,
        tmp_bucket=os.environ.get("TMP_BUCKET_NAME", ""),
        region=_aws_region(),
        hub_account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        identity_center_start_url=os.environ.get("AWS_CONSOLE_START_URL") or None,
        identity_center_role_name=os.environ.get("AWS_CONSOLE_ROLE_NAME") or None,
        approved_plan_pointer_key=approved_plan_pointer_key,
    )


def _pipeline_plan_focus_enabled(event: dict[str, Any]) -> bool:
    return event.get("pipeline_plan_focus") is True


def _should_post_final_summary(
    action: str,
    render_items: list[dict[str, Any]],
    *,
    pipeline_plan_focus: bool = False,
) -> bool:
    """Post a linked multi-folder summary for report and any multi-folder execution (e.g. plan all)."""
    if pipeline_plan_focus:
        return False
    return action == "report" or len(render_items) > 1


def _render_early_placeholder(event: dict[str, Any]) -> dict[str, Any]:
    if not _uses_github_pr(event):
        return {
            "early_placeholder_rendered": False,
            "early_placeholder_skipped": "registry-only ingress",
        }
    webhook = event["webhook_info"]
    repo, pr = webhook["repo_name"], webhook["pr_number"]
    commit_hash = webhook.get("commit_hash") or ""
    console_url = _console_url(event.get("execution_arn"))
    if not console_url or not commit_hash:
        return {
            "early_placeholder_rendered": False,
            "early_placeholder_skipped": "missing console_url or commit_hash",
        }
    run_id = _resolve_run_id(event)
    token = get_github_token(event["settings"]["ssm_openci_tf_github_token"])
    client = GitHubClient(token)
    body = _with_command_context(
        event,
        status_comment_in_progress(commit_hash, console_url, run_id),
        run_id=run_id,
    )
    comment_id = client.create_comment(repo, pr, body)
    cleanup_warnings: list[str] = []
    action = str(event.get("action") or webhook.get("action") or "plan")
    if not defer_command_comment_cleanup(action):
        cleanup_warnings.extend(
            delete_acknowledged_command_comment(
                client,
                repo,
                webhook.get("comment_id") if isinstance(webhook.get("comment_id"), int) else None,
            )
        )
    return _with_cleanup_warnings(
        {"early_placeholder_rendered": True, "status_comment_id": comment_id},
        cleanup_warnings,
    )


def _render_placeholder(event: dict[str, Any]) -> dict[str, Any]:
    if not _uses_github_pr(event):
        return {
            "placeholder_rendered": False,
            "placeholder_skipped": "registry-only ingress",
        }
    webhook = event["webhook_info"]
    repo, pr = webhook["repo_name"], webhook["pr_number"]
    commit_hash = webhook.get("commit_hash") or ""
    action = event.get("action", "plan")
    run_id = (
        event.get("run_id")
        if isinstance(event.get("run_id"), str) and event.get("run_id")
        else None
    )
    console_url = _console_url(event.get("execution_arn"))
    skipped = [
        item
        for item in event.get("skipped", [])
        if isinstance(item, dict) and item.get("folder")
    ]
    folders = [
        item
        for item in event.get("map_items", [])
        if isinstance(item, dict) and item.get("folder")
    ]
    if not folders and not skipped:
        return {
            "placeholder_rendered": False,
            "placeholder_skipped": "no launched folders",
        }
    token = get_github_token(event["settings"]["ssm_openci_tf_github_token"])
    client = GitHubClient(token)
    pipeline_plan_focus = _pipeline_plan_focus_enabled(event)
    for item in folders:
        folder = item["folder"]
        if action in {"apply", "destroy"} and console_url and run_id:
            grace_seconds = _mutation_grace_seconds(item, action)
            body = mutation_status_comment_in_progress(
                action=action,
                folder=folder,
                commit_hash=commit_hash,
                grace_seconds=grace_seconds,
                console_url=console_url,
                run_id=run_id,
            )
        else:
            body = pending_plan_comment(folder, item["account_id"], commit_hash, action)
        if not pipeline_plan_focus:
            _delete_and_repost(
                client,
                repo,
                pr,
                _with_command_context(
                    event,
                    body,
                    run_id=run_id,
                    account_id=item.get("account_id")
                    if isinstance(item.get("account_id"), str)
                    else None,
                ),
                action,
                folder,
            )
    if pipeline_plan_focus:
        preview_label = (
            "destroy order" if action == "plan_destroy" else "apply order"
        )
        pending_body = (
            f"> **Pipeline plan preview · {preview_label}**\n\n"
            f"Planning {len(folders)} folder{'s' if len(folders) != 1 else ''}…"
        )
        _delete_and_repost(
            client,
            repo,
            pr,
            _with_command_context(
                event,
                pending_body,
                run_id=run_id,
                include_account=False,
                include_source_plan_run_id=False,
                include_metadata=False,
            ),
            action,
            "all",
            report_all=_summary_uses_report_all(action),
        )
    else:
        _delete_and_repost(
            client,
            repo,
            pr,
            _with_command_context(
                event,
                pending_summary(folders, skipped, action=action),
                run_id=run_id,
                include_account=False,
                include_source_plan_run_id=False,
            ),
            action,
            "all",
            report_all=_summary_uses_report_all(action),
        )
    cleanup_warnings: list[str] = []
    if not defer_command_comment_cleanup(action):
        cleanup_warnings.extend(
            delete_acknowledged_command_comment(
                client,
                repo,
                webhook.get("comment_id") if isinstance(webhook.get("comment_id"), int) else None,
            )
        )
    return _with_cleanup_warnings({"placeholder_rendered": True}, cleanup_warnings)


def _resolved_no_op_reason(event: dict[str, Any]) -> str | None:
    if "no_op_reason" not in event or event["no_op_reason"] is None:
        return None
    reason = event["no_op_reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("no_op_reason must be a non-empty string when present")
    return reason.strip()


def _render_no_op(event: dict[str, Any], reason: str) -> dict[str, Any]:
    run_id = _resolve_run_id(event)
    action = event["action"]
    cleanup_warnings: list[str] = []
    if _uses_github_pr(event):
        webhook = event["webhook_info"]
        repo, pr = webhook["repo_name"], webhook["pr_number"]
        token = get_github_token(event["settings"]["ssm_openci_tf_github_token"])
        client = GitHubClient(token)
        _delete_and_repost(
            client,
            repo,
            pr,
            _with_command_context(event, f"## Plan skipped\n\n{reason}.", run_id=run_id),
            "plan",
            "all",
        )
        cleanup_warnings = _delete_transient_status_comment(client, repo, pr, run_id) or []
        cleanup_warnings.extend(
            delete_acknowledged_command_comment(
                client,
                repo,
                webhook.get("comment_id") if isinstance(webhook.get("comment_id"), int) else None,
            )
        )
    _update_run_registry(event, [], action, skipped=[])
    return _with_cleanup_warnings(
        {
            "execution_failed": False,
            "rendered": True,
            "no_op": True,
            "no_op_reason": reason,
        },
        cleanup_warnings,
    )


def _uses_github_pr(event: dict[str, Any]) -> bool:
    webhook = event["webhook_info"]
    notification = (
        event.get("notification_target") or webhook.get("notification_target") or {}
    )
    if isinstance(notification, dict) and notification.get("type") == "registry":
        return False
    return isinstance(webhook.get("pr_number"), int)


def _confirm_token_from_event(event: dict[str, Any]) -> str | None:
    token = event.get("consumed_confirm_token") or event.get("confirm_token")
    return token if isinstance(token, str) and token else None


def _intent_record_matches_event(record: dict[str, Any], event: dict[str, Any]) -> bool:
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return False
    trigger_id = webhook.get("trigger_id")
    pr_number = webhook.get("pr_number")
    action = event.get("action") or webhook.get("action")
    if (
        not isinstance(trigger_id, str)
        or not isinstance(pr_number, int)
        or not isinstance(action, str)
        or not action
    ):
        return False
    return intent_record_matches_current_request(
        record,
        trigger_id=trigger_id,
        pr_number=pr_number,
        action=action,
    )


def _event_with_recovered_intent_metadata(event: dict[str, Any]) -> dict[str, Any]:
    token = _confirm_token_from_event(event)
    if token is None:
        return event
    needs_record = not isinstance(event.get("requested_comment_id"), int) or not isinstance(
        event.get("intent_comment_id"), int
    )
    needs_body = not isinstance(event.get("requested_comment_body"), str)
    if not needs_record and not needs_body:
        return event
    record = get_intent_record(token)
    if not record or not _intent_record_matches_event(record, event):
        return event
    recovered: dict[str, Any] = {}
    if not isinstance(event.get("requested_comment_id"), int) and isinstance(
        record.get("requested_comment_id"), int
    ):
        recovered["requested_comment_id"] = record["requested_comment_id"]
    if not isinstance(event.get("intent_comment_id"), int) and isinstance(
        record.get("intent_comment_id"), int
    ):
        recovered["intent_comment_id"] = record["intent_comment_id"]
    if not isinstance(event.get("requested_comment_body"), str) and isinstance(
        record.get("requested_comment_body"), str
    ):
        recovered["requested_comment_body"] = record["requested_comment_body"]
    if not recovered:
        return event
    return {**event, **recovered}


def _cleanup_terminal_mutation_comments_once(
    client: GitHubClient,
    repo: str,
    pr: int,
    event: dict[str, Any],
) -> list[str]:
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        raise TypeError("mutation cleanup requires webhook_info")
    if str(event.get("action") or webhook.get("action") or "") not in {
        "apply",
        "destroy",
    }:
        return []
    explicit_ids = [
        webhook.get("comment_id") if isinstance(webhook.get("comment_id"), int) else None,
        event.get("requested_comment_id")
        if isinstance(event.get("requested_comment_id"), int)
        else None,
        event.get("intent_comment_id")
        if isinstance(event.get("intent_comment_id"), int)
        else None,
    ]
    warnings = delete_acknowledged_command_comments(client, repo, explicit_ids)
    stale_token = _confirm_token_from_event(event)
    if stale_token is not None:
        warnings.extend(
            delete_stale_confirm_token_comments(
                client,
                repo,
                pr,
                stale_token,
                exclude_comment_ids={
                    comment_id
                    for comment_id in explicit_ids
                    if isinstance(comment_id, int)
                },
                should_delete_body=lambda body: body_is_confirm_intent_comment(
                    body, stale_token
                ),
            )
        )
    return warnings


def _cleanup_terminal_mutation_comments(
    client: GitHubClient,
    repo: str,
    pr: int,
    event: dict[str, Any],
) -> list[str]:
    for attempt in range(_TERMINAL_CLEANUP_ATTEMPTS):
        try:
            return _cleanup_terminal_mutation_comments_once(client, repo, pr, event)
        except requests.RequestException:
            if attempt == _TERMINAL_CLEANUP_ATTEMPTS - 1:
                raise
            time.sleep(_TERMINAL_CLEANUP_RETRY_SECONDS)
    raise RuntimeError("terminal mutation cleanup retry loop exhausted")


def _render_pipeline_failure(event: dict[str, Any]) -> dict[str, Any]:
    event = _event_with_recovered_intent_metadata(event)
    run_id = _resolve_run_id(event)
    failure = redact_and_bound_terminal_evidence(event.get("pipeline_failure"))
    if not isinstance(failure, dict):
        raise TypeError("pipeline_failure is required")
    failed_step = failure.get("failed_step")
    if not isinstance(failed_step, str) or not failed_step:
        raise ValueError("pipeline_failure.failed_step is required")
    action = failure.get("action")
    if action is None:
        failure_label = failed_step
    elif isinstance(action, str) and action:
        failure_label = f"{failed_step} ({action})"
    else:
        raise ValueError("pipeline_failure.action must be a non-empty string when present")
    console_url = _console_url(event.get("execution_arn"))
    webhook_for_action = event.get("webhook_info")
    event_action = str(
        event.get("action")
        or (webhook_for_action.get("action") if isinstance(webhook_for_action, dict) else "")
        or ""
    )
    if _uses_github_pr(event):
        webhook = event["webhook_info"]
        repo, pr = webhook["repo_name"], webhook["pr_number"]
        token = get_github_token(event["settings"]["ssm_openci_tf_github_token"])
        client = GitHubClient(token)
        link = console_url or "the Step Functions console"
        body = f" openci-tf pipeline failed at {failure_label} — see execution {link}"
        _delete_and_repost_unmanaged(
            client,
            repo,
            pr,
            _with_command_context(
                event,
                body,
                run_id=run_id,
                comments_removed=event_action in {"apply", "destroy"},
            ),
            "pipeline-failure",
        )
        cleanup_warnings = _delete_transient_status_comment(client, repo, pr, run_id) or []
        cleanup_warnings.extend(
            _cleanup_terminal_mutation_comments(client, repo, pr, event)
        )
        return _with_cleanup_warnings(
            {"pipeline_failure_rendered": True, "failed_step": failed_step},
            cleanup_warnings,
        )
    return {"pipeline_failure_rendered": True, "failed_step": failed_step}


def _normalization_state(event: dict[str, Any], purpose: str) -> dict[str, Any]:
    state = event.get("state")
    if not isinstance(state, dict):
        raise TypeError(f"{purpose} requires state")
    return state


def _normalize_folder_execution(event: dict[str, Any]) -> dict[str, object]:
    """Collapse one nested execution envelope into a bounded Map outcome."""
    state = _normalization_state(event, "folder outcome normalization")
    folder = state["folder"]
    account_id = state["account_id"]
    execution_id = state["execution_id"]
    attempt = state["attempt"]
    step_index = state.get("step_index") if isinstance(state.get("step_index"), int) else None
    if "child_execution" not in state:
        outcome = build_outer_map_outcome(
            folder=folder,
            account_id=account_id,
            execution_id=execution_id,
            status="infrastructure_error",
            succeeded=False,
            error="nested execution failed",
            attempt=attempt,
            step_index=step_index,
        )
    elif isinstance(state["child_execution"], dict):
        output = state["child_execution"].get("Output")
        child_execution_id = output.get("exec_id") if isinstance(output, dict) else None
        if isinstance(child_execution_id, str) and child_execution_id:
            outcome = build_outer_map_outcome(
                folder=folder,
                account_id=account_id,
                execution_id=child_execution_id,
                output=output,
                step_index=step_index,
            )
        else:
            outcome = build_outer_map_outcome(
                folder=folder,
                account_id=account_id,
                execution_id=execution_id,
                status="infrastructure_error",
                succeeded=False,
                error="malformed child execution output",
                attempt=attempt,
                step_index=step_index,
            )
    else:
        outcome = build_outer_map_outcome(
            folder=folder,
            account_id=account_id,
            execution_id=execution_id,
            status="infrastructure_error",
            succeeded=False,
            error="malformed child execution output",
            attempt=attempt,
            step_index=step_index,
        )
    validate_outer_map_outcome(outcome)
    return outcome


def _collect_step_outcomes(event: dict[str, Any]) -> dict[str, Any]:
    state = _normalization_state(event, "step outcome collection")
    raw_step_outcomes = event.get("step_outcomes")
    if not isinstance(raw_step_outcomes, list):
        raise TypeError("step_outcomes must be a list")
    raw_step_index = state.get("step_index")
    if not isinstance(raw_step_index, int) or isinstance(raw_step_index, bool):
        raise ValueError("state.step_index must be an integer")
    raw_step_count = state.get("step_count")
    if not isinstance(raw_step_count, int) or isinstance(raw_step_count, bool):
        raise ValueError("state.step_count must be an integer")
    prior_outcomes = state.get("outcomes")
    if not isinstance(prior_outcomes, list):
        raise TypeError("state.outcomes must be a list")
    collected: list[dict[str, Any]] = []
    step_failed = False
    for item in raw_step_outcomes:
        if not isinstance(item, dict):
            raise TypeError("step outcome must be an object")
        outcome = {**item, "step_index": raw_step_index}
        validate_outer_map_outcome(outcome)
        collected.append(outcome)
        normalized = _outcome(outcome)
        status = str(normalized.get("status") or "")
        if status in _FAILED_OUTCOME_STATUSES or normalized.get("succeeded") is False:
            step_failed = True
    next_step_index = raw_step_index + 1
    map_items = state.get("map_items")
    if not isinstance(map_items, list):
        raise TypeError("state.map_items must be a list")
    raw_steps = state.get("steps")
    steps = raw_steps if isinstance(raw_steps, list) else None
    next_items = _items_for_step(
        [item for item in map_items if isinstance(item, dict)],
        next_step_index,
        steps=steps,
    )
    skipped = list(state.get("skipped") or [])
    if step_failed:
        for item in map_items:
            if not isinstance(item, dict):
                raise TypeError("state.map_items must contain objects")
            item_step_index = item.get("step_index")
            if isinstance(item_step_index, int) and item_step_index > raw_step_index:
                skipped.append(
                    {
                        "folder": item["folder"],
                        "account_id": item["account_id"],
                        "execution_id": item["e"],
                        "status": "skipped",
                        "reply": "not run",
                        "step_index": item_step_index,
                    }
                )
    return {
        **state,
        "outcomes": prior_outcomes + collected,
        "step_outcomes": [],
        "step_index": next_step_index,
        "current_step_items": next_items,
        "step_failed": step_failed,
        "skipped": skipped,
    }


def _normalize_config_resolution_error(event: dict[str, Any]) -> dict[str, Any]:
    """Build the bounded render input for a caught configuration error."""
    state = _normalization_state(event, "configuration error normalization")
    outcome: dict[str, object] = {
        "folder": "config",
        "status": "infrastructure_error",
        "error": "configuration resolution failed",
    }
    validate_outer_map_outcome(outcome)
    return {
        "webhook_info": state["webhook_info"],
        "settings": state["settings"],
        "run_id": state["run_id"],
        "notification_target": state["notification_target"],
        "action": state["action"],
        "deadline_at": state.get("deadline_at"),
        "config_resolution_failed": True,
        "steps": state.get("steps", []),
        "step_index": state.get("step_index", 0),
        "step_count": state.get("step_count", 0),
        "outcomes": [outcome],
        "skipped": [],
        "no_op_reason": None,
        "folders": state.get("folders", []),
        "all_flag": state.get("all_flag", False),
        "affected_flag": state.get("affected_flag", False),
        "requested_comment_id": state.get("requested_comment_id"),
        "requested_comment_body": state.get("requested_comment_body"),
        "intent_comment_id": state.get("intent_comment_id"),
        "consumed_confirm_token": state.get("consumed_confirm_token"),
        "confirm_token": state.get("confirm_token"),
        "pipeline_plan_focus": state.get("pipeline_plan_focus"),
        "pipeline_mutation_plan_first": state.get("pipeline_mutation_plan_first"),
        "pending_mutation_action": state.get("pending_mutation_action"),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    logger.info("render handler invoked", extra={"action": event.get("action")})
    if event.get("normalize_config_error") is True:
        return _normalize_config_resolution_error(event)
    if event.get("normalize_folder_outcome") is True:
        return _normalize_folder_execution(event)
    if event.get("collect_step_outcomes") is True:
        return _collect_step_outcomes(event)
    if event.get("pipeline_failure"):
        return _render_pipeline_failure(event)
    if event.get("early_placeholder"):
        return _render_early_placeholder(event)
    if event.get("placeholder"):
        return _render_placeholder(event)
    event = _event_with_recovered_intent_metadata(event)
    no_op_reason = _resolved_no_op_reason(event)
    if no_op_reason is not None:
        return _render_no_op(event, no_op_reason)
    webhook = event["webhook_info"]
    commit_hash = webhook.get("commit_hash") or ""
    action = event.get("action", "plan")
    outcomes = [_outcome(item) for item in event.get("outcomes", [])]
    skipped_items = list(event.get("skipped", []))
    seen_folders: set[str] = set()
    deduped_outcomes: list[dict[str, Any]] = []
    for outcome in outcomes:
        folder = str(outcome.get("folder") or "")
        if folder and folder not in seen_folders:
            seen_folders.add(folder)
            deduped_outcomes.append(outcome)
    outcomes = deduped_outcomes
    render_items = outcomes + [_outcome(item) for item in skipped_items]
    pipeline_footer = _pipeline_apply_footer(event, action, outcomes, skipped_items)
    if not _uses_github_pr(event):
        table = cast(Any, boto3.resource("dynamodb")).Table(
            os.environ["LOCKS_TABLE_NAME"]
        )
        repo = webhook["repo_name"]
        for outcome in outcomes:
            folder = outcome.get("folder")
            execution_id = outcome.get("execution_id", outcome.get("exec_id"))
            if isinstance(folder, str) and isinstance(execution_id, str):
                run_lock.release(table, repo, folder, execution_id)
        _update_run_registry(event, outcomes, action, skipped=skipped_items)
        terminal = _terminal_status(outcomes, skipped_items)
        return {
            "execution_failed": terminal != "succeeded",
            "registry_only": True,
            "rendered": True,
        }
    repo, pr = webhook["repo_name"], webhook["pr_number"]
    run_id = _resolve_run_id(event)
    scoped_pr, _ = _scoped_pr_context(
        run_id, pr if isinstance(pr, int) else None, action
    )
    console_url = _console_url(event.get("execution_arn"))
    token = get_github_token(event["settings"]["ssm_openci_tf_github_token"])
    client = GitHubClient(token)
    artifacts_by_folder: dict[str, dict[str, str]] = {}
    folder_urls: dict[str, str] = {}
    table = cast(Any, boto3.resource("dynamodb")).Table(os.environ["LOCKS_TABLE_NAME"])
    pipeline_plan_focus = _pipeline_plan_focus_enabled(event)
    pipeline_mutation = _is_pipeline_mutation(event, action)
    plan_pending = event.get("pipeline_mutation_plan_first") is True
    mutation_action = _pipeline_mutation_action(event, action)
    for outcome in render_items:
        folder = outcome["folder"]
        execution_id = outcome.get("execution_id", outcome.get("exec_id"))
        try:
            if _should_list_execution_artifacts(outcome):
                prefix = _artifact_list_prefix(
                    repo_name=repo,
                    run_id=run_id,
                    folder=folder,
                    action=action,
                    pr_number=pr if isinstance(pr, int) else None,
                )
                artifacts = list_text_prefix(
                    os.environ["TMP_BUCKET_NAME"],
                    prefix,
                    MAX_ARTIFACT_BYTES,
                    ALLOWED_ARTIFACT_CONTENT_TYPES,
                )
                existing_names = frozenset(artifacts)
            else:
                artifacts = {}
                existing_names = frozenset()
            artifacts_by_folder[folder] = artifacts
            validated_plan_metadata = _plan_artifact_metadata(
                outcome, action, webhook, run_id, pr_number=scoped_pr
            )
            approved_plan_pointer_key = None
            if validated_plan_metadata is not None and scoped_pr is not None:
                _, pointer_type = _scoped_pr_context(
                    run_id, pr if isinstance(pr, int) else None, action
                )
                if pointer_type is not None:
                    approved_plan_pointer_key = pr_pointer_key(
                        repo_name=repo,
                        pr_number=scoped_pr,
                        folder_path=folder,
                        pointer_type=pointer_type,
                    )
            source_plan_run_id = (
                _source_plan_run_id(outcome)
                if action in {"apply", "destroy"}
                else None
            )
            if not pipeline_plan_focus and not pipeline_mutation:
                comment_id = _delete_and_repost(
                    client,
                    repo,
                    pr,
                    _with_command_context(
                        event,
                        _append_footer(
                            _render_folder_body(
                                folder,
                                outcome,
                                artifacts,
                                action=action,
                                commit_hash=commit_hash,
                                console_url=console_url,
                                run_id=run_id,
                                repo=repo,
                                render_items=render_items,
                                pr_number=scoped_pr,
                                existing_names=existing_names,
                                approved_plan_pointer_key=approved_plan_pointer_key,
                            ),
                            pipeline_footer,
                        ),
                        run_id=run_id,
                        comments_removed=True,
                        account_id=str(outcome.get("account_id") or "")
                        if isinstance(outcome.get("account_id"), str)
                        else None,
                        source_plan_run_id=source_plan_run_id,
                    ),
                    action,
                    folder,
                    emit_marker=should_emit_comment_object_marker(action, terminal=True),
                )
                folder_urls[folder] = comment_url(repo, pr, comment_id)
        finally:
            if isinstance(execution_id, str) and execution_id:
                run_lock.release(table, repo, folder, execution_id)
    if pipeline_mutation:
        aggregate_body, checkpoint_rows, existing_comment_id = _pipeline_mutation_aggregate_body(
            event,
            action=action,
            outcomes=outcomes,
            artifacts_by_folder=artifacts_by_folder,
            commit_hash=commit_hash,
            footer=None if plan_pending else pipeline_footer,
            plan_pending=plan_pending,
        )
        identity = _pipeline_aggregate_identity(event, action)
        comment_id = _upsert_managed_comment(
            client,
            repo,
            pr,
            _with_command_context(
                event,
                aggregate_body,
                run_id=run_id,
                comments_removed=not plan_pending,
                include_account=False,
                include_source_plan_run_id=False,
                include_metadata=False,
            ),
            mutation_action,
            "all",
            report_all=False,
            existing_comment_id=existing_comment_id,
            emit_marker=should_emit_comment_object_marker(mutation_action, terminal=True),
        )
        if identity is not None and os.environ.get("RUN_REGISTRY_TABLE_NAME"):
            from src.platform.aws.run_registry.pipeline_aggregate import (
                save_pipeline_aggregate_state,
            )

            save_pipeline_aggregate_state(
                **identity,
                comment_id=comment_id,
                checkpoint_rows=checkpoint_rows,
            )
    elif pipeline_plan_focus:
        steps = event.get("steps") if isinstance(event.get("steps"), list) else None
        preview_body = pipeline_plan_preview_comment(
            outcomes,
            artifacts_by_folder,
            action=action,
            steps=steps,
        )
        _delete_and_repost(
            client,
            repo,
            pr,
            _with_command_context(
                event,
                preview_body,
                run_id=run_id,
                comments_removed=True,
                include_account=False,
                include_source_plan_run_id=False,
                include_metadata=False,
            ),
            action,
            "all",
            report_all=_summary_uses_report_all(action),
            emit_marker=should_emit_comment_object_marker(action, terminal=True),
        )
    elif _should_post_final_summary(action, render_items):
        _delete_and_repost(
            client,
            repo,
            pr,
            _with_command_context(
                event,
                _append_footer(
                    summary(
                        render_items,
                        artifacts_by_folder,
                        action=action,
                        folder_urls=folder_urls,
                        commit_hash=commit_hash,
                        console_url=console_url,
                        steps=event.get("steps") if isinstance(event.get("steps"), list) else None,
                    ),
                    pipeline_footer,
                ),
                run_id=run_id,
                comments_removed=True,
                include_account=False,
                include_source_plan_run_id=False,
            ),
            action,
            "all",
            report_all=_summary_uses_report_all(action),
            emit_marker=should_emit_comment_object_marker(action, terminal=True),
        )
    else:
        _delete_generated_comment(
            client,
            repo,
            pr,
            action,
            "all",
            report_all=_summary_uses_report_all(action),
        )
    cleanup_warnings = _delete_transient_status_comment(client, repo, pr, _resolve_run_id(event)) or []
    if action in {"apply", "destroy"}:
        cleanup_warnings.extend(
            _cleanup_terminal_mutation_comments(client, repo, pr, event)
        )
    else:
        cleanup_warnings.extend(
            delete_acknowledged_command_comment(
                client,
                repo,
                webhook.get("comment_id") if isinstance(webhook.get("comment_id"), int) else None,
            )
        )
    _update_run_registry(event, outcomes, action, skipped=skipped_items)
    terminal = _terminal_status(outcomes, skipped_items)
    if action == "report" and isinstance(pr, int):
        _publish_report_all_pointer(
            repo_name=repo,
            pr_number=pr,
            run_id=run_id,
            terminal=terminal,
        )
    logger.info("render handler completed", extra={"run_id": run_id, "action": action})
    return _with_cleanup_warnings(
        {"execution_failed": terminal != "succeeded", "rendered": True},
        cleanup_warnings,
    )
