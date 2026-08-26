# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub PR command comment cleanup helpers."""

from __future__ import annotations

from unittest.mock import Mock

import requests

from src.platform.github.command_comment_cleanup import (
    delete_acknowledged_command_comment,
    delete_stale_confirm_token_comments,
    defer_command_comment_cleanup,
)


class _Client:
    def __init__(self, *, delete_error: Exception | None = None, matches: list[int] | None = None):
        self.deleted: list[int] = []
        self._delete_error = delete_error
        self._matches = matches or []

    def delete_comment(self, _repo: str, comment_id: int) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append(comment_id)

    def find_comment_ids_by_body_substring(self, _repo: str, _pr: int, needle: str) -> list[int]:
        if needle == "confirm abc123":
            return list(self._matches)
        return []


def test_delete_acknowledged_command_comment_treats_missing_as_non_fatal():
    response = Mock(status_code=404)
    client = _Client(delete_error=requests.HTTPError(response=response))
    warnings = delete_acknowledged_command_comment(client, "o/r", 99)
    assert warnings == []


def test_delete_stale_confirm_token_comments_skips_excluded_ids():
    client = _Client(matches=[10, 11, 12])
    warnings = delete_stale_confirm_token_comments(
        client,
        "o/r",
        1,
        "abc123",
        exclude_comment_ids={11},
    )
    assert warnings == []
    assert client.deleted == [10, 12]


def test_delete_stale_confirm_token_comments_noop_without_token():
    client = _Client(matches=[10])
    warnings = delete_stale_confirm_token_comments(client, "o/r", 1, None)
    assert warnings == []
    assert client.deleted == []


def test_defer_command_comment_cleanup_for_apply_and_destroy():
    assert defer_command_comment_cleanup("apply") is True
    assert defer_command_comment_cleanup("destroy") is True
    assert defer_command_comment_cleanup("plan") is False
