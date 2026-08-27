# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Delete acknowledged raw openci-tf user command comments by explicit comment id."""

from __future__ import annotations

import requests

from src.platform.github.client import GitHubClient


def delete_acknowledged_command_comment(
    client: GitHubClient,
    repo: str,
    comment_id: int | None,
) -> list[str]:
    """Delete one user command comment after a bot replacement comment is posted."""
    if not isinstance(comment_id, int):
        return []
    try:
        client.delete_comment(repo, comment_id)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            return []
        raise
    return []


def delete_acknowledged_command_comments(
    client: GitHubClient,
    repo: str,
    comment_ids: list[int | None],
) -> list[str]:
    """Delete multiple acknowledged command comments; raises on non-404 failures."""
    warnings: list[str] = []
    seen: set[int] = set()
    for comment_id in comment_ids:
        if not isinstance(comment_id, int) or comment_id in seen:
            continue
        seen.add(comment_id)
        warnings.extend(delete_acknowledged_command_comment(client, repo, comment_id))
    return warnings


def delete_stale_confirm_token_comments(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    token: str | None,
    *,
    exclude_comment_ids: set[int] | None = None,
) -> list[str]:
    """Delete bot-authored PR comments still containing a one-time confirm token.

    Only comments written by the token owner (the bot login) are swept. A human
    comment that merely quotes ``confirm <token>`` is never deleted by content.
    Non-404 deletion failures raise.
    """
    if not isinstance(token, str) or not token.strip():
        return []
    excluded = exclude_comment_ids or set()
    needle = f"confirm {token.strip()}"
    bot_login = client.token_login()
    warnings: list[str] = []
    for comment_id, author_login in client.find_comments_by_body_substring(repo, pr_number, needle):
        if comment_id in excluded or author_login != bot_login:
            continue
        warnings.extend(delete_acknowledged_command_comment(client, repo, comment_id))
    return warnings


def defer_command_comment_cleanup(action: str) -> bool:
    """Apply/destroy user commands are removed only after the terminal mutation comment."""
    return action in {"apply", "destroy"}
