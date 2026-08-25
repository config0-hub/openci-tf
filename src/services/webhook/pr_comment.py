# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post PR comments from webhook-shaped settings and context."""
from __future__ import annotations

from typing import Any

from src.platform.aws.ssm import get_github_token
from src.platform.github.client import GitHubClient


def post_pr_comment(webhook_info: dict[str, Any], settings: dict[str, Any], body: str) -> None:
    pr_number = webhook_info.get("pr_number")
    repo = webhook_info.get("repo_name")
    if not isinstance(pr_number, int) or not isinstance(repo, str):
        return
    token = get_github_token(settings["ssm_openci_tf_github_token"])
    GitHubClient(token).create_comment(repo, pr_number, body)
