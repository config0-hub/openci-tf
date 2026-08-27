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
    MAX_AUDIT_BODY_CHARS,
    MAX_AUDIT_ROWS,
    append_audit_row,
    canonical_audit_rows,
    format_command_audit_comment,
    format_commands_run_marker,
    parse_audit_created_timestamp,
    parse_audit_rows,
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
    lock_version = _acquire_with_backoff(lock_table, repo, pr_number, holder)
    try:
        return _record_locked(
            client,
            repo,
            pr_number,
            command_text=command_text,
            status=status,
            delivery_id=delivery_id,
            when=when,
            lock_table=lock_table,
            holder=holder,
            lock_version=lock_version,
        )
    finally:
        audit_lock.release(lock_table, repo, pr_number, holder)


def _acquire_with_backoff(lock_table: Any, repo: str, pr_number: int, holder: str) -> int:
    for index, sleep_seconds in enumerate((*_LOCK_RETRY_SLEEPS, None)):
        try:
            return audit_lock.acquire(lock_table, repo, pr_number, holder, int(time.time()))
        except LockHeldError:
            if sleep_seconds is None:
                raise
            time.sleep(sleep_seconds)
    raise LockHeldError(f"audit lock held for {repo}#{pr_number}")


def _bot_authored_marker_ids(
    client: GitHubClient, repo: str, pr_number: int, marker: str
) -> list[int]:
    bot_login = client.token_login()
    return sorted(
        {
            comment_id
            for comment_id, author_login in client.find_comments_by_body_substring(
                repo, pr_number, marker
            )
            if author_login == bot_login
        }
    )


def _record_locked(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    command_text: str,
    status: str,
    delivery_id: str | None,
    when: datetime | None,
    lock_table: Any,
    holder: str,
    lock_version: int,
) -> int:
    marker = format_commands_run_marker(repo, pr_number)
    version = lock_version
    for attempt in range(3):
        existing_ids = _bot_authored_marker_ids(client, repo, pr_number, marker)
        existing_id = existing_ids[0] if existing_ids else None
        existing_body = ""
        if existing_id is not None:
            existing_body = client.get_comment_body(repo, existing_id) or ""
        if len(existing_ids) > 1:
            try:
                version = audit_lock.fence(
                    lock_table, repo, pr_number, holder, version, int(time.time())
                )
            except LockHeldError:
                if attempt == 2:
                    raise
                version = _acquire_with_backoff(lock_table, repo, pr_number, holder)
                continue
            existing_body = _merge_existing_audit_comments(
                client, repo, pr_number, existing_ids
            )
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
                try:
                    version = audit_lock.fence(
                        lock_table, repo, pr_number, holder, version, int(time.time())
                    )
                except LockHeldError:
                    if attempt == 2:
                        raise
                    version = _acquire_with_backoff(lock_table, repo, pr_number, holder)
                    continue
                client.update_comment(repo, existing_id, body)
            return existing_id
        version = audit_lock.fence(
            lock_table, repo, pr_number, holder, version, int(time.time())
        )
        created_id = client.create_comment(repo, pr_number, body)
        return _merge_duplicate_audit_comments(client, repo, pr_number, marker, created_id)
    raise LockHeldError(f"audit lock fence failed for {repo}#{pr_number}")


def _merge_existing_audit_comments(
    client: GitHubClient, repo: str, pr_number: int, marker_ids: list[int]
) -> str:
    """Merge audit rows into the lowest comment id and delete duplicate comments."""
    keeper_id = marker_ids[0]
    body = client.get_comment_body(repo, keeper_id) or ""
    created_at = parse_audit_created_timestamp(body) or ""
    rows = parse_audit_rows(body)
    for extra_id in marker_ids[1:]:
        extra_body = client.get_comment_body(repo, extra_id) or ""
        rows.extend(parse_audit_rows(extra_body))
    rows = canonical_audit_rows(rows)[-MAX_AUDIT_ROWS:]
    body = format_command_audit_comment(
        created_at=created_at,
        rows=rows,
        repo_name=repo,
        pr_number=pr_number,
    )
    while len(body) > MAX_AUDIT_BODY_CHARS and len(rows) > 1:
        rows = rows[1:]
        body = format_command_audit_comment(
            created_at=created_at,
            rows=rows,
            repo_name=repo,
            pr_number=pr_number,
        )
    if len(body) > MAX_AUDIT_BODY_CHARS:
        raise ValueError("audit comment exceeds the body limit with a single row")
    client.update_comment(repo, keeper_id, body)
    for extra_id in marker_ids[1:]:
        client.delete_comment(repo, extra_id)
    return body


def _merge_duplicate_audit_comments(
    client: GitHubClient, repo: str, pr_number: int, marker: str, created_id: int
) -> int:
    """Collapse audit comments created concurrently into the lowest comment id."""
    marker_ids = sorted(
        set(_bot_authored_marker_ids(client, repo, pr_number, marker)) | {created_id}
    )
    if len(marker_ids) == 1:
        return marker_ids[0]
    _merge_existing_audit_comments(client, repo, pr_number, marker_ids)
    return marker_ids[0]
