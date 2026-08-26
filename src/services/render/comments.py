# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub comment lifecycle helpers for render."""

from __future__ import annotations

from typing import Any

import requests

from src.domain.formatters.artifacts import (
    bound_comment,
    command_context_block,
    mutation_command_context_block,
    status_comment_marker_prefix,
)
from src.domain.github.comment_object_id import (
    comment_type_for_action,
    folder_value_for_comment,
    format_comment_object_marker,
    legacy_folder_suffix,
    legacy_opaque_tag,
    legacy_summary_suffix,
)
from src.platform.github.client import GitHubClient, comment_url


def _uses_github_pr(event: dict[str, Any]) -> bool:
    webhook = event["webhook_info"]
    notification = (
        event.get("notification_target") or webhook.get("notification_target") or {}
    )
    if isinstance(notification, dict) and notification.get("type") == "registry":
        return False
    return isinstance(webhook.get("pr_number"), int)


def _command_context_from_event(
    event: dict[str, Any],
    run_id: str | None = None,
    *,
    comments_removed: bool = False,
) -> str:
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return ""
    action = str(event.get("action") or webhook.get("action") or "plan")
    folders = event.get("folders")
    folder_list = list(folders) if isinstance(folders, list) else []
    all_flag = bool(event.get("all_flag"))
    affected_flag = bool(event.get("affected_flag"))
    comment_id = webhook.get("comment_id")
    comment_link = None
    pr_number = webhook.get("pr_number")
    repo_name = webhook.get("repo_name")
    if isinstance(comment_id, int) and isinstance(pr_number, int) and isinstance(repo_name, str):
        comment_link = comment_url(repo_name, pr_number, comment_id)
    resolved_run_id = run_id
    if not resolved_run_id:
        raw_run_id = event.get("run_id")
        resolved_run_id = raw_run_id if isinstance(raw_run_id, str) and raw_run_id else None
    commit_hash = webhook.get("commit_hash")
    requested_comment_body = event.get("requested_comment_body")
    if action in {"apply", "destroy"} and isinstance(requested_comment_body, str):
        requested_comment_id = event.get("requested_comment_id")
        requested_link = None
        if (
            isinstance(requested_comment_id, int)
            and isinstance(pr_number, int)
            and isinstance(repo_name, str)
        ):
            requested_link = comment_url(repo_name, pr_number, requested_comment_id)
        confirmation_body = (
            webhook.get("comment_body") if isinstance(webhook.get("comment_body"), str) else None
        )
        return mutation_command_context_block(
            action=action,
            requested_comment_body=requested_comment_body,
            requested_comment_id=requested_comment_id if isinstance(requested_comment_id, int) else None,
            requested_comment_link=requested_link,
            confirmation_comment_body=confirmation_body,
            confirmation_comment_id=comment_id if isinstance(comment_id, int) else None,
            confirmation_comment_link=comment_link,
            run_id=resolved_run_id,
            commit_hash=commit_hash if isinstance(commit_hash, str) else None,
            comments_removed=comments_removed,
        )
    return command_context_block(
        action=action,
        folders=folder_list,
        all_flag=all_flag,
        affected_flag=affected_flag,
        comment_body=webhook.get("comment_body") if isinstance(webhook.get("comment_body"), str) else None,
        comment_id=comment_id if isinstance(comment_id, int) else None,
        comment_link=comment_link,
        run_id=resolved_run_id,
        commit_hash=commit_hash if isinstance(commit_hash, str) else None,
        comment_removed=comments_removed,
        pipeline=webhook.get("pipeline") if isinstance(webhook.get("pipeline"), str) else None,
        pipeline_step=webhook.get("pipeline_step_index")
        if isinstance(webhook.get("pipeline_step_index"), int)
        else None,
    )


def _with_command_context(
    event: dict[str, Any],
    body: str,
    run_id: str | None = None,
    *,
    comments_removed: bool = False,
) -> str:
    if not _uses_github_pr(event):
        return body
    context = _command_context_from_event(event, run_id=run_id, comments_removed=comments_removed)
    if not context:
        return body
    return f"{context}\n\n---\n\n{body}"


def _with_cleanup_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if warnings:
        result = {**result, "comment_cleanup_warnings": warnings}
    return result


def _delete_and_repost_unmanaged(
    client: GitHubClient, repo: str, pr: int, body: str, suffix: str
) -> int:
    tag = legacy_opaque_tag(repo, pr, suffix)
    return client.delete_and_repost(
        repo, pr, bound_comment(body, suffix=f"\n\n#{tag}"), tag
    )


def _managed_comment_marker(
    repo: str,
    pr: int,
    action: str,
    folder: str,
    *,
    report_all: bool = False,
) -> str:
    comment_type = comment_type_for_action(action, report_all=report_all)
    folder_value = folder_value_for_comment(
        comment_type, folder if not report_all else "all"
    )
    return format_comment_object_marker(repo, pr, comment_type, folder_value)


def _legacy_suffix_for_managed_comment(
    action: str, folder: str, *, report_all: bool = False
) -> str:
    if report_all:
        return legacy_summary_suffix()
    return legacy_folder_suffix(folder)


def _delete_managed_comment(
    client: GitHubClient,
    repo: str,
    pr: int,
    marker: str,
    *,
    legacy_suffix: str,
) -> None:
    legacy_tag = legacy_opaque_tag(repo, pr, legacy_suffix)
    for comment_id in client.find_comments_by_tag(repo, pr, legacy_tag):
        client.delete_comment(repo, comment_id)
    for comment_id in client.find_comments_by_tag(repo, pr, marker):
        client.delete_comment(repo, comment_id)


def _delete_and_repost(
    client: GitHubClient,
    repo: str,
    pr: int,
    body: str,
    action: str,
    folder: str,
    *,
    report_all: bool = False,
    emit_marker: bool = True,
) -> int:
    marker = _managed_comment_marker(repo, pr, action, folder, report_all=report_all)
    _delete_managed_comment(
        client,
        repo,
        pr,
        marker,
        legacy_suffix=_legacy_suffix_for_managed_comment(
            action, folder, report_all=report_all
        ),
    )
    suffix = f"\n\n{marker}" if emit_marker else ""
    return client.create_comment(repo, pr, bound_comment(body, suffix=suffix))


def _delete_generated_comment(
    client: GitHubClient,
    repo: str,
    pr: int,
    action: str,
    folder: str,
    *,
    report_all: bool = False,
) -> None:
    marker = _managed_comment_marker(repo, pr, action, folder, report_all=report_all)
    _delete_managed_comment(
        client,
        repo,
        pr,
        marker,
        legacy_suffix=_legacy_suffix_for_managed_comment(
            action, folder, report_all=report_all
        ),
    )


def _delete_transient_status_comment(
    client: GitHubClient, repo: str, pr: int, run_id: str
) -> list[str]:
    prefix = status_comment_marker_prefix(run_id)
    warnings: list[str] = []
    for comment_id in client.find_comments_by_tag(repo, pr, prefix):
        try:
            client.delete_comment(repo, comment_id)
        except requests.RequestException as error:
            warnings.append(f"failed to delete transient status comment {comment_id}: {error}")
    return warnings
