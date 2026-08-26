# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub client helpers."""

from unittest.mock import Mock

from src.platform.github.client import GitHubClient


def test_find_comment_ids_by_body_substring_empty_needle_returns_empty():
    client = GitHubClient("token")
    client.session = Mock()
    assert client.find_comment_ids_by_body_substring("org/repo", 7, "") == []
    client.session.get.assert_not_called()


def test_find_comments_by_body_substring_returns_author_logins():
    client = GitHubClient("token")
    client.session = Mock()
    pages = [
        Mock(status_code=200, json=lambda: [
            {"id": 1, "body": "please confirm ab12cd", "user": {"login": "alice"}},
            {"id": 2, "body": "tf apply confirm ab12cd", "user": {"login": "openci-bot"}},
            {"id": 3, "body": "unrelated", "user": {"login": "bob"}},
        ]),
        Mock(status_code=200, json=lambda: []),
    ]
    client.session.get.side_effect = pages
    assert client.find_comments_by_body_substring("org/repo", 7, "confirm ab12cd") == [
        (1, "alice"),
        (2, "openci-bot"),
    ]


def test_token_login_is_cached_per_client():
    client = GitHubClient("token")
    client.session = Mock()
    client.session.get.return_value = Mock(status_code=200, json=lambda: {"login": "openci-bot"})
    assert client.token_login() == "openci-bot"
    assert client.token_login() == "openci-bot"
    assert client.session.get.call_count == 1
