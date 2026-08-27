# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CodeBuild links must identify the hub account required by the AWS console."""

from __future__ import annotations

from src.domain.formatters.artifacts import (
    mutation_status_comment_in_progress,
    mutation_terminal_comment,
)
from src.services.render.comments import _with_command_context
from src.services.run_folder import publish_mutation_progress


def test_mutation_progress_labels_codebuild_hub_account() -> None:
    body = mutation_status_comment_in_progress(
        action="apply",
        folder="infra/ec2",
        commit_hash="a" * 40,
        grace_seconds=15,
        console_url="https://example.test/states",
        codebuild_url="https://example.test/codebuild",
        codebuild_account_id="REPLACE_MAIN_ACCOUNT",
        run_id="run-1",
        now=1,
    )

    assert "hub account `REPLACE_MAIN_ACCOUNT`" in body
    assert "switch the AWS console to this account first" in body


def test_codebuild_progress_keeps_placeholder_command_context(monkeypatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    run_id = "run-1"
    repo_name = "org/repo"
    captured: list[str] = []
    placeholder_event = {
        "action": "apply",
        "folders": ["infra/ec2"],
        "requested_comment_id": 10,
        "requested_comment_body": "tf apply infra/ec2",
        "intent_comment_id": 11,
        "webhook_info": {
            "repo_name": repo_name,
            "pr_number": 7,
            "comment_id": 55,
            "comment_body": "tf apply confirm <redacted>",
            "commit_hash": "a" * 40,
        },
    }
    placeholder = _with_command_context(
        placeholder_event,
        mutation_status_comment_in_progress(
            action="apply",
            folder="infra/ec2",
            commit_hash="a" * 40,
            grace_seconds=15,
            console_url="https://example.test/states",
            run_id=run_id,
        ),
        run_id=run_id,
    )

    class Client:
        def __init__(self, _token):
            pass

        def delete_and_repost(self, _repo, _pr, body, _tag):
            captured.append(body)
            return 99

    monkeypatch.setattr(
        publish_mutation_progress,
        "get_run",
        lambda _run_id: {
            "notification_target": {"type": "github_pr", "pr_number": 7},
        },
    )
    monkeypatch.setattr(publish_mutation_progress, "get_github_token", lambda _path: "token")
    monkeypatch.setattr(publish_mutation_progress, "GitHubClient", Client)

    result = publish_mutation_progress.publish_codebuild_link(
        run_id=run_id,
        repo_name=repo_name,
        folder="infra/ec2",
        action="apply",
        commit_hash="a" * 40,
        grace_seconds=15,
        outer_execution_arn="arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        codebuild_project="openci-tf-worker",
        codebuild_build_id="openci-tf-worker:11111111-2222-3333-4444-555555555555",
        ssm_github_token_path="/openci-tf/clone-token/test",
        command_context={
            "comment_id": 55,
            "comment_body": "tf apply confirm <redacted>",
            "requested_comment_id": 10,
            "requested_comment_body": "tf apply infra/ec2",
            "intent_comment_id": 11,
        },
    )

    assert result["updated"] is True
    assert placeholder.startswith("### openci-tf command")
    assert captured[0].startswith("### openci-tf command")
    assert "requested command: `tf apply infra/ec2`" in captured[0]
    assert "## Apply in progress" in captured[0]


def test_mutation_terminal_labels_codebuild_hub_account() -> None:
    body = mutation_terminal_comment(
        action="apply",
        folder="infra/ec2",
        account_id="REPLACE_SECONDARY_ACCOUNT",
        commit_hash="a" * 40,
        succeeded=True,
        pinned_plan_artifact="plan.tfplan",
        console_url="https://example.test/states",
        codebuild_url="https://example.test/codebuild",
        codebuild_account_id="REPLACE_MAIN_ACCOUNT",
        plan_show_text=None,
        plan_show_pointer=None,
    )

    assert "hub account `REPLACE_MAIN_ACCOUNT`" in body
    assert "switch the AWS console to this account first" in body
