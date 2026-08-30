# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub comment lifecycle helpers for render."""

from __future__ import annotations

from typing import Any, Callable, cast

import requests

from src.domain.formatters.artifacts import (
    bound_comment,
    metadata_section,
    prominent_command_header,
    status_comment_marker_prefix,
)
from src.domain.github.comment_object_id import (
    body_has_legacy_opaque_tag,
    body_has_status_comment_marker_prefix,
    body_has_trailing_managed_marker,
    comment_type_for_action,
    folder_value_for_comment,
    format_comment_object_marker,
    legacy_folder_suffix,
    legacy_opaque_tag,
    legacy_summary_suffix,
)
from src.platform.github.client import GitHubClient, comment_url
from src.platform.github.command_comment_cleanup import delete_acknowledged_command_comment


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
    account_id: str | None = None,
    source_plan_run_id: str | None = None,
    include_account: bool = True,
    include_source_plan_run_id: bool = True,
) -> str:
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return ""
    action = str(event.get("action") or webhook.get("action") or "plan")
    if action == "report":
        return ""
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
        return metadata_section(
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
            account_id=account_id,
            source_plan_run_id=source_plan_run_id,
            include_account=include_account,
            include_source_plan_run_id=include_source_plan_run_id,
        )
    return metadata_section(
        action=action,
        folders=folder_list,
        all_flag=all_flag,
        affected_flag=affected_flag,
        comment_body=webhook.get("comment_body") if isinstance(webhook.get("comment_body"), str) else None,
        comment_id=comment_id if isinstance(comment_id, int) else None,
        comment_link=comment_link,
        run_id=resolved_run_id,
        commit_hash=commit_hash if isinstance(commit_hash, str) else None,
        comments_removed=comments_removed,
        pipeline=webhook.get("pipeline") if isinstance(webhook.get("pipeline"), str) else None,
        pipeline_step=webhook.get("pipeline_step_index")
        if isinstance(webhook.get("pipeline_step_index"), int)
        else None,
        account_id=account_id,
        source_plan_run_id=source_plan_run_id,
        include_account=include_account,
        include_source_plan_run_id=include_source_plan_run_id,
    )


def _with_command_context(
    event: dict[str, Any],
    body: str,
    run_id: str | None = None,
    *,
    comments_removed: bool = False,
    account_id: str | None = None,
    source_plan_run_id: str | None = None,
    include_account: bool = True,
    include_source_plan_run_id: bool = True,
    include_metadata: bool = True,
) -> str:
    if not _uses_github_pr(event):
        return body
    webhook = event.get("webhook_info")
    if not isinstance(webhook, dict):
        return body
    action = str(event.get("action") or webhook.get("action") or "plan")
    if action == "report":
        return body
    folders = event.get("folders")
    folder_list = list(folders) if isinstance(folders, list) else []
    commit_hash = webhook.get("commit_hash")
    header = prominent_command_header(
        action=action,
        folders=folder_list,
        all_flag=bool(event.get("all_flag")),
        affected_flag=bool(event.get("affected_flag")),
        comment_body=webhook.get("comment_body") if isinstance(webhook.get("comment_body"), str) else None,
        pipeline=webhook.get("pipeline") if isinstance(webhook.get("pipeline"), str) else None,
        pipeline_step=webhook.get("pipeline_step_index")
        if isinstance(webhook.get("pipeline_step_index"), int)
        else None,
        requested_comment_body=event.get("requested_comment_body")
        if isinstance(event.get("requested_comment_body"), str)
        else None,
        commit_hash=commit_hash if isinstance(commit_hash, str) else None,
    )
    parts = [header, body]
    if include_metadata:
        metadata = _command_context_from_event(
            event,
            run_id=run_id,
            comments_removed=comments_removed,
            account_id=account_id,
            source_plan_run_id=source_plan_run_id,
            include_account=include_account,
            include_source_plan_run_id=include_source_plan_run_id,
        )
        if metadata:
            parts.append(metadata)
    return "\n\n".join(parts)


def _with_cleanup_warnings(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if warnings:
        result = {**result, "comment_cleanup_warnings": warnings}
    return result


_CommentDetailSearch = Callable[[str, int, str], list[dict[str, str | int]]]
_BodySubstringSearch = Callable[[str, int, str], list[tuple[int, str]]]


def _candidate_comment_details(
    client: GitHubClient, repo: str, pr: int, needle: str
) -> list[dict[str, str | int]]:
    detail_search = getattr(client, "find_comment_details_by_body_substring", None)
    if callable(detail_search):
        return list(cast(_CommentDetailSearch, detail_search)(repo, pr, needle))
    body_search = getattr(client, "find_comments_by_body_substring", None)
    get_body = getattr(client, "get_comment_body", None)
    if not callable(body_search) or not callable(get_body):
        return []
    details: list[dict[str, str | int]] = []
    for comment_id, author_login in cast(_BodySubstringSearch, body_search)(
        repo, pr, needle
    ):
        if type(comment_id) is not int:
            raise ValueError("GitHub comment search returned no integer id")
        body = get_body(repo, comment_id)
        details.append(
            {
                "id": comment_id,
                "author_login": author_login if isinstance(author_login, str) else "",
                "body": body if isinstance(body, str) else "",
            }
        )
    return details


def _bot_authored_comment_ids(
    client: GitHubClient,
    repo: str,
    pr: int,
    needle: str,
    *,
    body_matches: Any,
) -> list[int]:
    token_login = getattr(client, "token_login", None)
    if not callable(token_login):
        return []
    bot_login = token_login()
    if not bot_login:
        return []
    ids: list[int] = []
    for comment in _candidate_comment_details(client, repo, pr, needle):
        if comment.get("author_login") != bot_login:
            continue
        body = comment.get("body")
        if not isinstance(body, str) or not body_matches(body):
            continue
        comment_id = comment.get("id")
        if type(comment_id) is not int:
            raise ValueError("GitHub comment search returned no integer id")
        ids.append(comment_id)
    return ids


def _delete_and_repost_unmanaged(
    client: GitHubClient, repo: str, pr: int, body: str, suffix: str
) -> int:
    tag = legacy_opaque_tag(repo, pr, suffix)
    for comment_id in _bot_authored_comment_ids(
        client,
        repo,
        pr,
        tag,
        body_matches=lambda body: body_has_legacy_opaque_tag(body, tag),
    ):
        client.delete_comment(repo, comment_id)
    return client.create_comment(repo, pr, bound_comment(body, suffix=f"\n\n#{tag}"))


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
    for comment_id in _bot_authored_comment_ids(
        client,
        repo,
        pr,
        legacy_tag,
        body_matches=lambda body: body_has_legacy_opaque_tag(body, legacy_tag),
    ):
        client.delete_comment(repo, comment_id)
    for comment_id in _bot_authored_comment_ids(
        client,
        repo,
        pr,
        marker,
        body_matches=lambda body: body_has_trailing_managed_marker(body, marker),
    ):
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


def _upsert_managed_comment(
    client: GitHubClient,
    repo: str,
    pr: int,
    body: str,
    action: str,
    folder: str,
    *,
    report_all: bool = False,
    existing_comment_id: int | None = None,
    emit_marker: bool = True,
) -> int:
    marker = _managed_comment_marker(repo, pr, action, folder, report_all=report_all)
    suffix = f"\n\n{marker}" if emit_marker else ""
    bounded = bound_comment(body, suffix=suffix)
    if isinstance(existing_comment_id, int) and existing_comment_id > 0:
        try:
            client.update_comment(repo, existing_comment_id, bounded)
            return existing_comment_id
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 404:
                raise
    _delete_managed_comment(
        client,
        repo,
        pr,
        marker,
        legacy_suffix=_legacy_suffix_for_managed_comment(
            action, folder, report_all=report_all
        ),
    )
    return client.create_comment(repo, pr, bounded)


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
    for comment_id in _bot_authored_comment_ids(
        client,
        repo,
        pr,
        prefix,
        body_matches=lambda body: body_has_status_comment_marker_prefix(body, prefix),
    ):
        delete_acknowledged_command_comment(client, repo, comment_id)
    return []
