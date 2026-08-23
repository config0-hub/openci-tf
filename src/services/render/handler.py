"""Render bounded execution outcomes into deterministic PR comments."""

from __future__ import annotations

import os
from typing import Any, cast

import boto3  # type: ignore[import-not-found]

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
    status_comment_in_progress,
    summary,
)
from src.domain.formatters.console_urls import step_functions_execution_url
from src.domain.locks import run_lock
from src.domain.run.outcome import normalize_map_outcome as _outcome
from src.domain.engine.summary import (
    build_outer_map_outcome,
    validate_outer_map_outcome,
)
from src.platform.aws.s3 import get_bounded_json, list_text_prefix
from src.platform.aws.ssm import get_github_token
from src.platform.github.client import GitHubClient, comment_url
from src.domain.github.comment_object_id import should_emit_comment_object_marker
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
)
from src.services.render.registry_update import (
    _resolve_run_id,
    _run_drift_detected as _registry_run_drift_detected,
    _terminal_status,
    _update_run_registry,
)

_FAILED_OUTCOME_STATUSES = frozenset({"failed", "infrastructure_error"})

logger = get_logger(__name__)

# Re-exported for existing unit tests that pin the render/registry seam.
_managed_comment_marker = _comments_managed_comment_marker
_run_drift_detected = _registry_run_drift_detected


def _summary_uses_report_all(action: str) -> bool:
    return action == "report"


def _console_url(execution_arn: str | None) -> str | None:
    if not isinstance(execution_arn, str) or not execution_arn:
        return None
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    return step_functions_execution_url(execution_arn, region=region)


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


def _pipeline_apply_footer(
    event: dict[str, Any],
    action: str,
    outcomes: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> str | None:
    if action != "apply":
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
        raise ValueError("invalid pipeline apply step metadata")
    if _terminal_status(outcomes, skipped) != "succeeded":
        return None
    if step_index < step_count:
        return f"next: tf apply pipeline {pipeline} step {step_index + 1}"
    return f"pipeline {pipeline} complete ({step_count} steps)"


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
) -> str:
    account_id = str(outcome.get("account_id") or "")
    status = str(outcome.get("status") or "")
    succeeded = outcome.get("succeeded") is True and status not in {
        "failed",
        "infrastructure_error",
        "in_progress",
        "skipped",
    }
    plan_artifact = "plan.tfplan" if action == "apply" else "destroy.plan.tfplan"
    plan_show = artifacts.get("plan-show.out", "")
    if not plan_show.strip() and succeeded:
        source_run_id = _source_plan_run_id(outcome)
        if source_run_id:
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
        account_id=account_id,
        commit_hash=commit_hash,
        succeeded=succeeded,
        pinned_plan_artifact=plan_artifact,
        console_url=console_url,
        codebuild_url=codebuild_url,
        codebuild_account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        plan_show_text=plan_show or None,
        plan_show_pointer=plan_pointer,
        error=terminal_error,
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
    manifest_s3_uri: str | None,
    pr_number: int | None = None,
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
        )
    return folder_comment(
        folder,
        outcome,
        artifacts,
        action=action,
        commit_hash=commit_hash,
        console_url=console_url,
        include_ci_details=len(render_items) == 1,
        manifest_s3_uri=manifest_s3_uri,
        run_id=run_id,
        repo_name=repo,
        pr_number=pr_number,
    )


def _should_post_final_summary(action: str, render_items: list[dict[str, Any]]) -> bool:
    """Post a linked multi-folder summary for report and any multi-folder execution (e.g. plan all)."""
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
    body = status_comment_in_progress(commit_hash, console_url, run_id)
    comment_id = client.create_comment(repo, pr, body)
    return {"early_placeholder_rendered": True, "status_comment_id": comment_id}


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
        _delete_and_repost(
            client,
            repo,
            pr,
            body,
            action,
            folder,
        )
    _delete_and_repost(
        client,
        repo,
        pr,
        pending_summary(folders, skipped),
        action,
        "all",
        report_all=_summary_uses_report_all(action),
    )
    return {"placeholder_rendered": True}


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
    if _uses_github_pr(event):
        webhook = event["webhook_info"]
        repo, pr = webhook["repo_name"], webhook["pr_number"]
        token = get_github_token(event["settings"]["ssm_openci_tf_github_token"])
        client = GitHubClient(token)
        _delete_and_repost(
            client,
            repo,
            pr,
            f"## Plan skipped\n\n{reason}.",
            action,
            "all",
        )
        _delete_transient_status_comment(client, repo, pr, run_id)
    _update_run_registry(event, [], action, skipped=[])
    return {
        "execution_failed": False,
        "rendered": True,
        "no_op": True,
        "no_op_reason": reason,
    }


def _uses_github_pr(event: dict[str, Any]) -> bool:
    webhook = event["webhook_info"]
    notification = (
        event.get("notification_target") or webhook.get("notification_target") or {}
    )
    if isinstance(notification, dict) and notification.get("type") == "registry":
        return False
    return isinstance(webhook.get("pr_number"), int)


def _render_pipeline_failure(event: dict[str, Any]) -> dict[str, Any]:
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
    if _uses_github_pr(event):
        webhook = event["webhook_info"]
        repo, pr = webhook["repo_name"], webhook["pr_number"]
        token = get_github_token(event["settings"]["ssm_openci_tf_github_token"])
        client = GitHubClient(token)
        link = console_url or "the Step Functions console"
        body = f" openci-tf pipeline failed at {failure_label} — see execution {link}"
        _delete_and_repost_unmanaged(client, repo, pr, body, "pipeline-failure")
        _delete_transient_status_comment(client, repo, pr, run_id)
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
    next_items = [
        item
        for item in map_items
        if isinstance(item, dict) and item.get("step_index") == next_step_index
    ]
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
        "config_resolution_failed": True,
        "steps": state.get("steps", []),
        "step_index": state.get("step_index", 0),
        "step_count": state.get("step_count", 0),
        "outcomes": [outcome],
        "skipped": [],
        "no_op_reason": None,
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
            else:
                artifacts = {}
            artifacts_by_folder[folder] = artifacts
            _plan_artifact_metadata(
                outcome, action, webhook, run_id, pr_number=scoped_pr
            )
            comment_id = _delete_and_repost(
                client,
                repo,
                pr,
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
                        manifest_s3_uri=outcome.get("manifest_s3_uri")
                        if isinstance(outcome.get("manifest_s3_uri"), str)
                        else (outcome.get("pointers") or {}).get("manifest")
                        if isinstance(outcome.get("pointers"), dict)
                        else None,
                        pr_number=scoped_pr,
                    ),
                    pipeline_footer,
                ),
                action,
                folder,
                emit_marker=should_emit_comment_object_marker(action, terminal=True),
            )
            folder_urls[folder] = comment_url(repo, pr, comment_id)
        finally:
            if isinstance(execution_id, str) and execution_id:
                run_lock.release(table, repo, folder, execution_id)
    if _should_post_final_summary(action, render_items):
        _delete_and_repost(
            client,
            repo,
            pr,
            _append_footer(
                summary(
                    render_items,
                    artifacts_by_folder,
                    folder_urls=folder_urls,
                    commit_hash=commit_hash,
                    console_url=console_url,
                    steps=event.get("steps") if isinstance(event.get("steps"), list) else None,
                ),
                pipeline_footer,
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
    _delete_transient_status_comment(client, repo, pr, _resolve_run_id(event))
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
    return {"execution_failed": terminal != "succeeded", "rendered": True}
