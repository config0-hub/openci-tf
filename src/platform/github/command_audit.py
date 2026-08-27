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
    audit_delivery_has_status,
    canonical_audit_rows,
    format_command_audit_comment,
    format_commands_run_marker,
    is_commands_run_audit_comment,
    migrate_legacy_audit_rows,
    parse_audit_created_timestamp,
    update_audit_row_status,
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


def update_command_audit_status(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    delivery_id: str,
    status: str,
    lock_table: Any,
    command_text: str | None = None,
) -> int:
    """Update an audit row status under the per-PR audit lock, recreating it if needed."""
    holder = uuid.uuid4().hex
    lock_version = _acquire_with_backoff(lock_table, repo, pr_number, holder)
    try:
        return _update_status_locked(
            client,
            repo,
            pr_number,
            delivery_id=delivery_id,
            status=status,
            lock_table=lock_table,
            holder=holder,
            lock_version=lock_version,
            command_text=command_text,
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


def _comment_details_by_body_substring(
    client: GitHubClient, repo: str, pr_number: int, marker: str
) -> list[dict[str, str | int]]:
    detail_search = getattr(client, "find_comment_details_by_body_substring", None)
    if callable(detail_search):
        return list(detail_search(repo, pr_number, marker))
    body_search = getattr(client, "find_comments_by_body_substring", None)
    get_body = getattr(client, "get_comment_body", None)
    if not callable(body_search) or not callable(get_body):
        return []
    details: list[dict[str, str | int]] = []
    for comment_id, author_login in body_search(repo, pr_number, marker):
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


def _bot_authored_marker_ids(
    client: GitHubClient, repo: str, pr_number: int, marker: str
) -> list[int]:
    bot_login = client.token_login()
    ids: set[int] = set()
    for comment in _comment_details_by_body_substring(client, repo, pr_number, marker):
        if comment.get("author_login") != bot_login:
            continue
        body = comment.get("body")
        if not isinstance(body, str) or not is_commands_run_audit_comment(body):
            continue
        comment_id = comment.get("id")
        if type(comment_id) is not int:
            raise ValueError("GitHub comment search returned no integer id")
        ids.add(comment_id)
    return sorted(ids)


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


def _update_status_locked(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    *,
    delivery_id: str,
    status: str,
    lock_table: Any,
    holder: str,
    lock_version: int,
    command_text: str | None,
) -> int:
    marker = format_commands_run_marker(repo, pr_number)
    version = lock_version
    for attempt in range(3):
        existing_ids = _bot_authored_marker_ids(client, repo, pr_number, marker)
        existing_id = existing_ids[0] if existing_ids else None
        existing_body = (
            client.get_comment_body(repo, existing_id)
            if existing_id is not None
            else None
        )
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
        body = update_audit_row_status(
            existing_body,
            delivery_id=delivery_id,
            status=status,
            repo_name=repo,
            pr_number=pr_number,
        )
        if not audit_delivery_has_status(body, delivery_id, status):
            if command_text is None:
                raise ValueError(
                    f"audit delivery row {delivery_id!r} is missing and no command_text was supplied"
                )
            body = append_audit_row(
                existing_body if existing_body and marker in existing_body else None,
                command_text=command_text,
                status=status,
                repo_name=repo,
                pr_number=pr_number,
                delivery_id=delivery_id,
            )
        if not audit_delivery_has_status(body, delivery_id, status):
            raise ValueError(
                f"audit delivery row {delivery_id!r} was not written as {status!r}"
            )
        if existing_id is not None and existing_body is not None:
            if body == existing_body:
                return existing_id
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
        if existing_id is not None and existing_body is None:
            return created_id
        return _merge_duplicate_audit_comments(client, repo, pr_number, marker, created_id)
    raise LockHeldError(f"audit lock fence failed for {repo}#{pr_number}")


def _merge_existing_audit_comments(
    client: GitHubClient, repo: str, pr_number: int, marker_ids: list[int]
) -> str:
    """Merge audit rows into the lowest comment id and delete duplicate comments."""
    candidates: list[tuple[int, str]] = []
    for comment_id in marker_ids:
        body = client.get_comment_body(repo, comment_id) or ""
        if is_commands_run_audit_comment(body):
            candidates.append((comment_id, body))
    if not candidates:
        raise ValueError("no structural audit comments to merge")
    keeper_id, body = candidates[0]
    created_at = parse_audit_created_timestamp(body) or ""
    rows = migrate_legacy_audit_rows(body, source_comment_id=keeper_id)
    for extra_id, extra_body in candidates[1:]:
        rows.extend(migrate_legacy_audit_rows(extra_body, source_comment_id=extra_id))
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
    for extra_id, _extra_body in candidates[1:]:
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
