# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Upsert durable PR command audit comments keyed by comment_object_id marker."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from src.core.errors import LockHeldError
from src.domain.formatters.command_audit import (
    append_audit_row,
    format_commands_run_marker,
    parse_audit_rows,
    parse_command_timestamp,
)
from src.platform.aws import audit_lock
from src.platform.github.client import GitHubClient

# Lock contention retries: sleeps sum to roughly five seconds before giving up.
_LOCK_RETRY_SLEEPS = (0.25, 0.5, 1.0, 1.5, 2.0)


def record_command_audit(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    command_text: str,
    status: str,
    delivery_id: str | None,
    lock_table: Any,
    when: datetime | None = None,
) -> int:
    """Append one audit row under the per-PR audit lock; create the comment when missing.

    A row that already carries ``delivery_id`` is not appended again, so a GitHub
    redelivery of the same webhook is idempotent.
    """
    holder = uuid.uuid4().hex
    _acquire_with_backoff(lock_table, repo, pr_number, holder)
    try:
        return _record_locked(
            client,
            repo,
            pr_number,
            command_text=command_text,
            status=status,
            delivery_id=delivery_id,
            when=when,
        )
    finally:
        audit_lock.release(lock_table, repo, pr_number, holder)


def _acquire_with_backoff(lock_table: Any, repo: str, pr_number: int, holder: str) -> None:
    for index, sleep_seconds in enumerate((*_LOCK_RETRY_SLEEPS, None)):
        try:
            audit_lock.acquire(lock_table, repo, pr_number, holder, int(time.time()))
            return
        except LockHeldError:
            if sleep_seconds is None:
                raise
            time.sleep(sleep_seconds)
    raise LockHeldError(f"audit lock held for {repo}#{pr_number}")


def _record_locked(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    command_text: str,
    status: str,
    delivery_id: str | None,
    when: datetime | None,
) -> int:
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
        delivery_id=delivery_id,
    )
    if existing_id is not None:
        if body != existing_body:
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
        for time_value, command_text, status, row_delivery_id in parse_audit_rows(extra_body):
            body = append_audit_row(
                body or None,
                command_text=command_text,
                status=status,
                when=parse_command_timestamp(time_value),
                repo_name=repo,
                pr_number=pr_number,
                delivery_id=row_delivery_id,
            )
    client.update_comment(repo, keeper_id, body)
    for extra_id in marker_ids[1:]:
        client.delete_comment(repo, extra_id)
    return keeper_id
