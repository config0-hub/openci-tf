"""Update mutation in-progress PR comments with CodeBuild links."""

from __future__ import annotations

import os
from typing import Any

from src.domain.formatters.console_urls import (
    codebuild_build_url,
    step_functions_execution_url,
)
from src.domain.formatters.artifacts import mutation_status_comment_in_progress
from src.platform.aws.run_registry import get_run
from src.platform.aws.ssm import get_github_token
from src.platform.github.client import GitHubClient, generate_search_tag


def _notification_pr(notification_target: object) -> tuple[str, int] | None:
    if not isinstance(notification_target, dict):
        return None
    if notification_target.get("type") != "github_pr":
        return None
    pr_number = notification_target.get("pr_number")
    if not isinstance(pr_number, int) or pr_number < 1:
        return None
    return "github_pr", pr_number


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
    body = mutation_status_comment_in_progress(
        action=action,
        folder=folder,
        commit_hash=commit_hash,
        grace_seconds=grace_seconds,
        console_url=console_url,
        codebuild_url=codebuild_url,
        codebuild_account_id=os.environ.get("ENGINE_CODEBUILD_ACCOUNT_ID") or None,
        run_id=run_id,
    )
    token = get_github_token(ssm_github_token_path)
    client = GitHubClient(token)
    tag = generate_search_tag(repo_name, pr_number, f"folder-{folder}")
    client.delete_and_repost(repo_name, pr_number, body, tag)
    return {"updated": True, "codebuild_url": codebuild_url}
