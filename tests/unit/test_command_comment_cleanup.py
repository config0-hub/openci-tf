# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub PR command comment cleanup helpers."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from src.domain.github.comment_object_id import (
    body_is_confirm_intent_comment,
    format_comment_object_marker,
)
from src.platform.github.command_comment_cleanup import (
    delete_acknowledged_command_comment,
    delete_stale_confirm_token_comments,
    defer_command_comment_cleanup,
)


class _Client:
    def __init__(
        self,
        *,
        delete_error: Exception | None = None,
        matches: list[tuple[int, str]] | None = None,
        details: list[dict[str, str | int]] | None = None,
    ):
        self.deleted: list[int] = []
        self._delete_error = delete_error
        self._matches = matches or []
        self._details = details

    def token_login(self) -> str:
        return "openci-bot"

    def delete_comment(self, _repo: str, comment_id: int) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append(comment_id)

    def find_comments_by_body_substring(
        self, _repo: str, _pr: int, needle: str
    ) -> list[tuple[int, str]]:
        if needle == "confirm abc123":
            return list(self._matches)
        return []

    def find_comment_details_by_body_substring(
        self, _repo: str, _pr: int, needle: str
    ) -> list[dict[str, str | int]]:
        if self._details is not None:
            return [item for item in self._details if needle in str(item.get("body", ""))]
        return [
            {
                "id": comment_id,
                "author_login": author_login,
                "body": _intent_body("apply", "abc123"),
                "created_at": "2026-01-01T00:00:00Z",
            }
            for comment_id, author_login in self.find_comments_by_body_substring(
                _repo, _pr, needle
            )
        ]


def _intent_body(action: str, token: str) -> str:
    return (
        "### openci-tf command\n"
        f"- command: `tf {action} infra/a`\n\n"
        "---\n\n"
        f"## tf {action} intent created\n\n"
        f"To proceed within 10 min: `tf {action} confirm {token}`"
    )


def _plan_result_body_with_raw_token(token: str) -> str:
    marker = format_comment_object_marker("o/r", 1, "plan", "infra/a")
    return (
        "<details>\n"
        "<summary>`infra/a` · 123456789012 · abcdef0 · Plan succeeded</summary>\n\n"
        "## Terraform: `infra/a` (123456789012)\n\n"
        "### Plan\n"
        "```diff\n"
        f"output message = \"confirm {token}\"\n"
        "```\n\n"
        "</details>\n\n"
        f"{marker}"
    )


def test_delete_acknowledged_command_comment_treats_missing_as_non_fatal():
    response = Mock(status_code=404)
    client = _Client(delete_error=requests.HTTPError(response=response))
    warnings = delete_acknowledged_command_comment(client, "o/r", 99)
    assert warnings == []


def test_delete_acknowledged_command_comment_raises_non_404():
    response = Mock(status_code=403)
    client = _Client(delete_error=requests.HTTPError(response=response))
    with pytest.raises(requests.HTTPError):
        delete_acknowledged_command_comment(client, "o/r", 99)


def test_delete_stale_confirm_token_comments_skips_excluded_ids():
    client = _Client(matches=[(10, "openci-bot"), (11, "openci-bot"), (12, "openci-bot")])
    warnings = delete_stale_confirm_token_comments(
        client,
        "o/r",
        1,
        "abc123",
        exclude_comment_ids={11},
        should_delete_body=lambda body: body_is_confirm_intent_comment(body, "abc123"),
    )
    assert warnings == []
    assert client.deleted == [10, 12]


def test_delete_stale_confirm_token_comments_never_deletes_human_comments():
    client = _Client(matches=[(10, "openci-bot"), (12, "alice")])
    warnings = delete_stale_confirm_token_comments(
        client,
        "o/r",
        1,
        "abc123",
        should_delete_body=lambda body: body_is_confirm_intent_comment(body, "abc123"),
    )
    assert warnings == []
    assert client.deleted == [10]


def test_delete_stale_confirm_token_comments_preserves_result_comments():
    client = _Client(
        details=[
            {
                "id": 101,
                "author_login": "openci-bot",
                "body": _plan_result_body_with_raw_token("abc123"),
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 102,
                "author_login": "openci-bot",
                "body": _intent_body("apply", "abc123"),
                "created_at": "2026-01-01T00:01:00Z",
            },
        ]
    )
    warnings = delete_stale_confirm_token_comments(
        client,
        "o/r",
        1,
        "abc123",
        should_delete_body=lambda body: body_is_confirm_intent_comment(body, "abc123"),
    )
    assert warnings == []
    assert client.deleted == [102]


def test_delete_stale_confirm_token_comments_noop_without_token():
    client = _Client(matches=[(10, "openci-bot")])
    warnings = delete_stale_confirm_token_comments(client, "o/r", 1, None)
    assert warnings == []
    assert client.deleted == []


def test_defer_command_comment_cleanup_for_apply_and_destroy():
    assert defer_command_comment_cleanup("apply") is True
    assert defer_command_comment_cleanup("destroy") is True
    assert defer_command_comment_cleanup("plan") is False
