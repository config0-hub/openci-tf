# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub comment lifecycle helpers for render."""

from __future__ import annotations

from src.domain.formatters.artifacts import (
    bound_comment,
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
from src.platform.github.client import GitHubClient


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
) -> None:
    prefix = status_comment_marker_prefix(run_id)
    for comment_id in client.find_comments_by_tag(repo, pr, prefix):
        client.delete_comment(repo, comment_id)
