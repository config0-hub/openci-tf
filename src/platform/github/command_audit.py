# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Upsert durable PR command audit comments keyed by comment_object_id marker."""

from __future__ import annotations

from datetime import datetime

from src.domain.formatters.command_audit import (
    append_audit_row,
    format_commands_run_marker,
    parse_audit_rows,
    parse_command_timestamp,
)
from src.platform.github.client import GitHubClient


def record_command_audit(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    command_text: str,
    status: str,
    when: datetime | None = None,
) -> int:
    """Append one audit row to the durable commands comment; create it when missing."""
    marker = format_commands_run_marker(repo, pr_number)
    existing_id = client.find_comment_by_tag(repo, pr_number, marker)
    existing_body = ""
    if existing_id is not None:
        existing_body = client.get_comment_body(repo, existing_id) or ""
    body = append_audit_row(
        existing_body or None,
        command_text=command_text,
        status=status,
        when=when,
        repo_name=repo,
        pr_number=pr_number,
    )
    if existing_id is not None:
        client.update_comment(repo, existing_id, body)
        return existing_id
    created_id = client.create_comment(repo, pr_number, body)
    return _merge_duplicate_audit_comments(client, repo, pr_number, marker, created_id)


def _merge_duplicate_audit_comments(
    client: GitHubClient, repo: str, pr_number: int, marker: str, created_id: int
) -> int:
    """Collapse audit comments created concurrently into the lowest comment id."""
    marker_ids = sorted(set(client.find_comments_by_tag(repo, pr_number, marker)) | {created_id})
    if len(marker_ids) == 1:
        return marker_ids[0]
    keeper_id = marker_ids[0]
    body = client.get_comment_body(repo, keeper_id) or ""
    for extra_id in marker_ids[1:]:
        extra_body = client.get_comment_body(repo, extra_id) or ""
        for time_value, command_text, status in parse_audit_rows(extra_body):
            body = append_audit_row(
                body or None,
                command_text=command_text,
                status=status,
                when=parse_command_timestamp(time_value),
                repo_name=repo,
                pr_number=pr_number,
            )
    client.update_comment(repo, keeper_id, body)
    for extra_id in marker_ids[1:]:
        client.delete_comment(repo, extra_id)
    return keeper_id
