# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Update mutation in-progress PR comments with CodeBuild links."""

from __future__ import annotations

import os
from typing import Any

from src.domain.formatters.console_urls import (
    codebuild_build_url,
    step_functions_execution_url,
)
from src.domain.formatters.artifacts import (
    bound_comment,
    metadata_section,
    mutation_status_comment_in_progress,
    status_comment_marker_prefix,
)
from src.platform.aws.run_registry import get_run
from src.platform.aws.ssm import get_github_token
from src.platform.github.client import GitHubClient, comment_url


def _notification_pr(notification_target: object) -> tuple[str, int] | None:
    if not isinstance(notification_target, dict):
        return None
    if notification_target.get("type") != "github_pr":
        return None
    pr_number = notification_target.get("pr_number")
    if not isinstance(pr_number, int) or pr_number < 1:
        return None
    return "github_pr", pr_number


def _int_field(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_field(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _progress_body_with_command_context(
    *,
    body: str,
    command_context: dict[str, Any] | None,
    repo_name: str,
    pr_number: int,
    action: str,
    folder: str,
    run_id: str,
    commit_hash: str,
) -> str:
    if not isinstance(command_context, dict):
        return body
    requested_body = _str_field(command_context.get("requested_comment_body"))
    requested_id = _int_field(command_context.get("requested_comment_id"))
    confirmation_body = _str_field(command_context.get("comment_body"))
    confirmation_id = _int_field(command_context.get("comment_id"))
    if requested_body is not None:
        context = metadata_section(
            action=action,
            requested_comment_body=requested_body,
            requested_comment_id=requested_id,
            requested_comment_link=comment_url(repo_name, pr_number, requested_id)
            if requested_id is not None
            else None,
            confirmation_comment_body=confirmation_body,
            confirmation_comment_id=confirmation_id,
            confirmation_comment_link=comment_url(repo_name, pr_number, confirmation_id)
            if confirmation_id is not None
            else None,
            run_id=run_id,
            commit_hash=commit_hash,
        )
    else:
        context = metadata_section(
            action=action,
            folders=[folder],
            comment_body=confirmation_body,
            comment_id=confirmation_id,
            comment_link=comment_url(repo_name, pr_number, confirmation_id)
            if confirmation_id is not None
            else None,
            run_id=run_id,
            commit_hash=commit_hash,
        )
    return f"{body}\n\n{context}"


def _replace_bot_progress_comment(
    client: GitHubClient,
    repo_name: str,
    pr_number: int,
    body: str,
    marker: str,
) -> int:
    """Delete only bot-authored progress comments that carry the run marker."""
    bot_login = client.token_login()
    for comment_id, author_login in client.find_comments_by_body_substring(
        repo_name,
        pr_number,
        marker,
    ):
        if author_login == bot_login:
            client.delete_comment(repo_name, comment_id)
    return client.create_comment(repo_name, pr_number, body)


def publish_codebuild_link(
    *,
    run_id: str,
    repo_name: str,
    folder: str,
    action: str,
    commit_hash: str,
    grace_seconds: int,
    outer_execution_arn: str | None,
    codebuild_project: str,
    codebuild_build_id: str,
    ssm_github_token_path: str,
    command_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Idempotently update the folder in-progress comment with a CodeBuild link."""
    run = get_run(run_id)
    if run is None:
        return {"updated": False, "reason": "run not found"}
    notification = run.get("notification_target")
    pr_info = _notification_pr(notification)
    if pr_info is None:
        return {"updated": False, "reason": "not a github_pr notification"}
    _, pr_number = pr_info
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    from src.domain.formatters.console_urls import is_valid_codebuild_build_id

    if not is_valid_codebuild_build_id(codebuild_build_id):
        return {"updated": False, "reason": "invalid codebuild build id"}
    codebuild_url = codebuild_build_url(
        codebuild_project,
        codebuild_build_id,
        region=region,
        account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        identity_center_start_url=os.environ.get("AWS_CONSOLE_START_URL") or None,
        identity_center_role_name=os.environ.get("AWS_CONSOLE_ROLE_NAME") or None,
    )
    console_url = (
        step_functions_execution_url(outer_execution_arn, region=region)
        if outer_execution_arn
        else None
    )
    if not console_url:
        stored = run.get("sfn_execution_arn")
        if isinstance(stored, str) and stored:
            console_url = step_functions_execution_url(stored, region=region)
    if not console_url:
        return {"updated": False, "reason": "missing outer execution ARN"}
    status_body = mutation_status_comment_in_progress(
        action=action,
        folder=folder,
        commit_hash=commit_hash,
        grace_seconds=grace_seconds,
        console_url=console_url,
        codebuild_url=codebuild_url,
        codebuild_account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        run_id=run_id,
    )
    body = _progress_body_with_command_context(
        body=status_body,
        command_context=command_context,
        repo_name=repo_name,
        pr_number=pr_number,
        action=action,
        folder=folder,
        run_id=run_id,
        commit_hash=commit_hash,
    )
    token = get_github_token(ssm_github_token_path)
    client = GitHubClient(token)
    marker = status_comment_marker_prefix(run_id)
    _replace_bot_progress_comment(client, repo_name, pr_number, bound_comment(body), marker)
    return {"updated": True, "codebuild_url": codebuild_url}
