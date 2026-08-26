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
