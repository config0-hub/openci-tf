# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GitHub client helpers."""

import io
from unittest.mock import Mock, patch

import pytest
import requests
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from src.platform.github.client import GitHubClient


def _github_response(status: int, body: bytes) -> HTTPResponse:
    return HTTPResponse(
        body=io.BytesIO(body),
        status=status,
        headers={"Content-Type": "application/json"},
        preload_content=False,
    )


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


def test_create_comment_retries_a_502_and_returns_the_comment_id():
    client = GitHubClient("token")
    answers = [
        _github_response(502, b'{"message": "Bad Gateway"}'),
        _github_response(201, b'{"id": 4242}'),
    ]
    with patch.object(HTTPConnectionPool, "_make_request", side_effect=answers) as make_request:
        comment_id = client.create_comment("org/repo", 56, "apply intent")
    assert comment_id == 4242
    assert make_request.call_count == 2


def test_create_comment_does_not_retry_a_4xx():
    client = GitHubClient("token")
    answers = [_github_response(422, b'{"message": "Validation Failed"}')]
    with patch.object(HTTPConnectionPool, "_make_request", side_effect=answers) as make_request:
        with pytest.raises(requests.HTTPError):
            client.create_comment("org/repo", 56, "apply intent")
    assert make_request.call_count == 1
