# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CodeBuild links must identify the hub account required by the AWS console."""

from __future__ import annotations

from src.domain.formatters.artifacts import (
    mutation_status_comment_in_progress,
    mutation_terminal_comment,
    status_comment_marker,
)
from src.platform.github import client as github_client
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

    class Client:
        def __init__(self, _token):
            pass

        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, _repo, _pr, _needle):
            return []

        def delete_comment(self, _repo, _comment_id):
            raise AssertionError("no existing bot comment should be deleted")

        def create_comment(self, _repo, _pr, body):
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
    assert "<summary>Metadata</summary>" in captured[0]
    assert "- Requested comment:" in captured[0]
    assert "## Apply in progress" in captured[0]


def test_codebuild_progress_replaces_only_bot_authored_status_marker(monkeypatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    run_id = "run-1"
    marker = status_comment_marker(run_id, now=1)
    comments = [
        {"id": 101, "body": f"human copy {marker}", "user": {"login": "alice"}},
        {"id": 102, "body": f"bot old {marker}", "user": {"login": "openci-bot"}},
    ]
    deleted: list[int] = []
    posted: list[str] = []

    class Response:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected HTTP status {self.status_code}")

    class Session:
        def __init__(self):
            self.headers = {}

        def get(self, url, params=None):
            if url == f"{github_client.GITHUB_API}/user":
                return Response({"login": "openci-bot"})
            if url.endswith("/repos/org/repo/issues/7/comments"):
                page = (params or {}).get("page", 1)
                return Response(comments if page == 1 else [])
            raise AssertionError(f"unexpected GET {url}")

        def delete(self, url):
            comment_id = int(url.rsplit("/", 1)[-1])
            deleted.append(comment_id)
            return Response({})

        def post(self, url, json):
            assert url.endswith("/repos/org/repo/issues/7/comments")
            posted.append(json["body"])
            return Response({"id": 202}, status_code=201)

    monkeypatch.setattr(github_client.requests, "Session", Session)
    monkeypatch.setattr(
        publish_mutation_progress,
        "get_run",
        lambda _run_id: {"notification_target": {"type": "github_pr", "pr_number": 7}},
    )
    monkeypatch.setattr(publish_mutation_progress, "get_github_token", lambda _path: "token")

    result = publish_mutation_progress.publish_codebuild_link(
        run_id=run_id,
        repo_name="org/repo",
        folder="infra/ec2",
        action="apply",
        commit_hash="a" * 40,
        grace_seconds=15,
        outer_execution_arn="arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        codebuild_project="openci-tf-worker",
        codebuild_build_id="openci-tf-worker:11111111-2222-3333-4444-555555555555",
        ssm_github_token_path="/openci-tf/clone-token/test",
    )

    assert result["updated"] is True
    assert deleted == [102]
    assert posted and "## Apply in progress" in posted[0]
    assert 101 not in deleted


def test_codebuild_progress_bounds_large_command_context(monkeypatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    captured: list[str] = []

    class Client:
        def __init__(self, _token):
            pass

        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, _repo, _pr, _needle):
            return []

        def delete_comment(self, _repo, _comment_id):
            raise AssertionError("no existing bot comment should be deleted")

        def create_comment(self, _repo, _pr, body):
            captured.append(body)
            return 99

    monkeypatch.setattr(
        publish_mutation_progress,
        "get_run",
        lambda _run_id: {"notification_target": {"type": "github_pr", "pr_number": 7}},
    )
    monkeypatch.setattr(publish_mutation_progress, "get_github_token", lambda _path: "token")
    monkeypatch.setattr(publish_mutation_progress, "GitHubClient", Client)
    huge_command = "tf " + (" " * 65_520) + "apply a"

    result = publish_mutation_progress.publish_codebuild_link(
        run_id="run-1",
        repo_name="org/repo",
        folder="a",
        action="apply",
        commit_hash="a" * 40,
        grace_seconds=15,
        outer_execution_arn="arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        codebuild_project="openci-tf-worker",
        codebuild_build_id="openci-tf-worker:11111111-2222-3333-4444-555555555555",
        ssm_github_token_path="/openci-tf/clone-token/test",
        command_context={"comment_id": 55, "comment_body": huge_command},
    )

    assert result["updated"] is True
    assert len(captured[0]) <= 65_536
    assert "<summary>Metadata</summary>" in captured[0]
    assert "Confirmation command:" in captured[0]


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
