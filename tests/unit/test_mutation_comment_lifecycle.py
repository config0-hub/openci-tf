# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for mutation progress comment lifecycle and terminal presentation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from src.domain.formatters.artifacts import (
    bound_comment,
    ensure_trailing_status_comment_marker,
    mutation_status_comment_in_progress,
    pipeline_mutation_aggregate_comment,
    status_comment_marker,
    status_comment_marker_prefix,
)
from src.domain.github.comment_object_id import body_has_status_comment_marker_prefix
from src.services.render import comments as render_comments
from src.services.render import handler as render_handler
from src.services.run_folder import publish_mutation_progress

_PR14_RUN_ID = "1788135341958.faf33c46"
_PR14_FOLDER = "terraform/primary/ap-northeast-1/06-sns-topic"
_PR14_COMMIT = "d16ec00" + "0" * 33
_PR14_CONSOLE_URL = (
    "https://console.aws.amazon.com/states/home?region=us-east-1"
    "#/executions/details/arn:aws:states:us-east-1:998038917735"
    ":execution:openci-tf-apply:1788135341958.faf33c46"
)
_PR14_CODEBUILD_URL = (
    "https://us-east-1.console.aws.amazon.com/codesuite/codebuild/us-east-1"
    "/projects/openci-tf-worker/build/openci-tf-worker:ebbee3c9-a363-4766-969e"
    "-c9b18510a48a/?region=us-east-1"
)
_PR14_COMMAND_CONTEXT = {
    "comment_id": 5472162844,
    "comment_body": "tf apply confirm <redacted>",
    "requested_comment_id": 5472156142,
    "requested_comment_body": f"tf apply {_PR14_FOLDER}",
}


def _production_progress_body(*, run_id: str = _PR14_RUN_ID) -> str:
    status_body = mutation_status_comment_in_progress(
        action="apply",
        folder=_PR14_FOLDER,
        commit_hash=_PR14_COMMIT,
        grace_seconds=15,
        console_url=_PR14_CONSOLE_URL,
        codebuild_url=_PR14_CODEBUILD_URL,
        codebuild_account_id="998038917735",
        run_id=run_id,
        now=1_788_138_982,
    )
    return publish_mutation_progress._progress_body_with_command_context(
        body=status_body,
        command_context=_PR14_COMMAND_CONTEXT,
        repo_name="williaumwu/openci-test-gitops",
        pr_number=14,
        action="apply",
        folder=_PR14_FOLDER,
        run_id=run_id,
        commit_hash=_PR14_COMMIT,
    )


def _pr14_buried_marker_body() -> str:
    """Captured PR #14 progress comment shape: marker before Metadata."""
    marker = status_comment_marker(_PR14_RUN_ID, now=1_788_138_982)
    return (
        f"\n## Apply in progress — `{_PR14_FOLDER}`\n"
        f"+ commit: `{_PR14_COMMIT[:7]}`\n"
        "+ grace period: 15s — stop the outer Step Functions execution during this wait "
        "to abort before CodeBuild starts\n"
        f"+ [Step Functions execution]({_PR14_CONSOLE_URL})\n"
        f"+ [CodeBuild job]({_PR14_CODEBUILD_URL}) — hub account `998038917735`; "
        "switch the AWS console to this account first\n"
        "+ status: in_progress\n\n"
        f"{marker}\n\n"
        "<details>\n<summary>Metadata</summary>\n\n"
        "- Confirmation command: `tf apply confirm <redacted>`\n"
        "- Requested comment: [5472156142](https://github.com/williaumwu/openci-test-gitops/pull/14#issuecomment-5472156142)\n"
        "- Confirmation comment: [5472162844](https://github.com/williaumwu/openci-test-gitops/pull/14#issuecomment-5472162844)\n"
        f"- Run ID: `{_PR14_RUN_ID}`\n\n"
        "</details>"
    )


class _MemoryGitHubClient:
    def __init__(self, comments: dict[int, str]):
        self._comments = comments
        self.deleted: list[int] = []

    def token_login(self) -> str:
        return "openci-bot"

    def find_comments_by_body_substring(self, _repo, _pr, needle: str):
        return [
            (comment_id, "openci-bot")
            for comment_id, body in self._comments.items()
            if needle in body
        ]

    def get_comment_body(self, _repo, comment_id: int) -> str:
        return self._comments[comment_id]

    def delete_comment(self, _repo, comment_id: int) -> None:
        self.deleted.append(comment_id)
        del self._comments[comment_id]


def test_pr14_buried_marker_is_not_structurally_deletable() -> None:
    body = _pr14_buried_marker_body()
    prefix = status_comment_marker_prefix(_PR14_RUN_ID)
    assert not body_has_status_comment_marker_prefix(body, prefix)


def test_production_progress_body_keeps_marker_trailing_after_metadata() -> None:
    body = bound_comment(_production_progress_body())
    prefix = status_comment_marker_prefix(_PR14_RUN_ID)
    assert "<summary>Metadata</summary>" in body
    assert body_has_status_comment_marker_prefix(body, prefix)
    assert body.rstrip().endswith(status_comment_marker(_PR14_RUN_ID, now=1_788_138_982))


def test_terminal_render_deletes_production_progress_comment(monkeypatch) -> None:
    progress_body = bound_comment(_production_progress_body())
    comments = {5472168302: progress_body}
    client = _MemoryGitHubClient(comments)

    render_comments._delete_transient_status_comment(
        client,
        "williaumwu/openci-test-gitops",
        14,
        _PR14_RUN_ID,
    )

    assert client.deleted == [5472168302]
    assert 5472168302 not in comments


def test_single_apply_terminal_render_removes_progress_and_posts_one_result(monkeypatch) -> None:
    progress_body = bound_comment(_production_progress_body())
    comments = {9001: progress_body}
    posted: list[tuple[str, str]] = []
    memory_client_holder: dict[str, _MemoryGitHubClient] = {}
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render_handler.boto3,
        "resource",
        lambda *_: SimpleNamespace(Table=lambda _: object()),
    )
    monkeypatch.setattr(
        render_handler,
        "list_text_prefix",
        lambda *_args, **_kwargs: {
            "plan-show.out": "Plan: 1 to add, 0 to change, 0 to destroy",
            "apply.out": "Apply complete!",
        },
    )
    monkeypatch.setattr(render_handler, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render_handler.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render_handler, "_update_run_registry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(render_handler, "_cleanup_terminal_mutation_comments", lambda *_args: [])

    class Client(_MemoryGitHubClient):
        def create_comment(self, repo, pr, body):
            posted.append((repo, body))
            return 9100

    memory_client_holder["client"] = Client(comments)
    monkeypatch.setattr(
        render_handler, "GitHubClient", lambda _: memory_client_holder["client"]
    )
    monkeypatch.setattr(
        render_handler,
        "_delete_and_repost",
        lambda _client, repo, _pr, body, action, folder, **kwargs: posted.append((f"{action}:{folder}", body)) or 9100,
    )

    result = render_handler.handler(
        {
            "run_id": _PR14_RUN_ID,
            "action": "apply",
            "webhook_info": {
                "repo_name": "williaumwu/openci-test-gitops",
                "pr_number": 14,
                "commit_hash": _PR14_COMMIT,
                "comment_body": "tf apply confirm <redacted>",
                "comment_id": 5472162844,
            },
            "settings": {"ssm_openci_tf_github_token": "/token"},
            "requested_comment_id": 5472156142,
            "outcomes": [
                {
                    "folder": _PR14_FOLDER,
                    "account_id": "998038917735",
                    "execution_id": "inner.apply.0",
                    "status": "succeeded",
                    "succeeded": True,
                }
            ],
            "skipped": [],
        },
        None,
    )

    assert result["rendered"] is True
    assert memory_client_holder["client"].deleted == [9001]
    assert any("Apply succeeded" in body for _, body in posted)
    assert all("in progress" not in body for _, body in posted)


def test_single_destroy_progress_body_cleanup(monkeypatch) -> None:
    run_id = "1788135638227.538c5fed"
    folder = _PR14_FOLDER
    status_body = mutation_status_comment_in_progress(
        action="destroy",
        folder=folder,
        commit_hash=_PR14_COMMIT,
        grace_seconds=60,
        console_url=_PR14_CONSOLE_URL.replace("openci-tf-apply", "openci-tf-destroy"),
        codebuild_url=_PR14_CODEBUILD_URL,
        codebuild_account_id="998038917735",
        run_id=run_id,
        now=1_788_139_320,
    )
    body = bound_comment(
        publish_mutation_progress._progress_body_with_command_context(
            body=status_body,
            command_context={
                "comment_id": 5472195654,
                "comment_body": "tf destroy confirm <redacted>",
                "requested_comment_id": 5472189476,
                "requested_comment_body": f"tf destroy {folder}",
            },
            repo_name="williaumwu/openci-test-gitops",
            pr_number=14,
            action="destroy",
            folder=folder,
            run_id=run_id,
            commit_hash=_PR14_COMMIT,
        )
    )
    comments = {9002: body}
    client = _MemoryGitHubClient(comments)
    render_comments._delete_transient_status_comment(
        client, "williaumwu/openci-test-gitops", 14, run_id
    )
    assert client.deleted == [9002]


def test_pipeline_apply_terminal_aggregate_omits_confirmation_note() -> None:
    body = pipeline_mutation_aggregate_comment(
        action="apply",
        pipeline="acceptance-b6be906",
        checkpoint_count=2,
        checkpoint_rows=[
            {
                "checkpoint_index": 1,
                "folder": "terraform/primary/ap-northeast-1/03-sqs",
                "account_id": "998038917735",
                "plan_show_text": "Plan: 1 to add",
                "pinned_plan_artifact": "plan.tfplan",
                "confirmation_status": "Confirmed ✅",
                "result_label": "Apply succeeded ✅",
                "succeeded": True,
            },
            {
                "checkpoint_index": 2,
                "folder": "terraform/primary/ap-northeast-1/05-s3-bucket",
                "account_id": "998038917735",
                "plan_show_text": "Plan: 3 to add",
                "pinned_plan_artifact": "plan.tfplan",
                "confirmation_status": "Confirmed ✅",
                "result_label": "Apply succeeded ✅",
                "succeeded": True,
            },
        ],
        footer="> [!NOTE]\n> Pipeline `acceptance-b6be906` complete (2 folders applied).",
        metadata_lines=[f"- Run ID: `{_PR14_RUN_ID}`"],
    )

    assert "> [!IMPORTANT]" not in body
    assert "> [!CAUTION]" not in body
    assert "**2 checkpoints** · **2 succeeded** · **0 failed**" in body
    assert body.count("<details>") == body.count("</details>")
    assert len(body.encode("utf-8")) <= 65_536


def test_pipeline_destroy_terminal_aggregate_omits_confirmation_note() -> None:
    body = pipeline_mutation_aggregate_comment(
        action="destroy",
        pipeline="acceptance-b6be906",
        checkpoint_count=2,
        checkpoint_rows=[
            {
                "checkpoint_index": 1,
                "folder": "terraform/primary/ap-northeast-1/05-s3-bucket",
                "account_id": "998038917735",
                "plan_show_text": "Plan: 0 to add, 0 to change, 3 to destroy",
                "pinned_plan_artifact": "destroy.plan.tfplan",
                "confirmation_status": "Confirmed ✅",
                "result_label": "Destroy succeeded ✅",
                "succeeded": True,
            },
            {
                "checkpoint_index": 2,
                "folder": "terraform/primary/ap-northeast-1/03-sqs",
                "account_id": "998038917735",
                "plan_show_text": "Plan: 0 to add, 0 to change, 1 to destroy",
                "pinned_plan_artifact": "destroy.plan.tfplan",
                "confirmation_status": "Confirmed ✅",
                "result_label": "Destroy succeeded ✅",
                "succeeded": True,
            },
        ],
        footer="> [!NOTE]\n> Pipeline `acceptance-b6be906` complete (2 folders destroyed).",
    )

    assert "> [!CAUTION]" not in body
    assert "fresh destroy plan" not in body.split("**2 checkpoints**", 1)[0]


def test_plan_pending_aggregate_shows_fresh_plan_safety_note() -> None:
    body = pipeline_mutation_aggregate_comment(
        action="apply",
        pipeline="acceptance-b6be906",
        checkpoint_count=2,
        checkpoint_rows=[
            {
                "checkpoint_index": 1,
                "folder": "terraform/primary/ap-northeast-1/03-sqs",
                "account_id": "998038917735",
                "plan_show_text": "Plan: 1 to add",
                "pinned_plan_artifact": "plan.tfplan",
                "confirmation_status": "Confirmed ✅",
                "result_label": "Apply succeeded ✅",
                "succeeded": True,
            },
            {
                "checkpoint_index": 2,
                "folder": "terraform/primary/ap-northeast-1/05-s3-bucket",
                "account_id": "998038917735",
                "plan_show_text": "Plan: 3 to add",
                "pinned_plan_artifact": "plan.tfplan",
                "confirmation_status": "Confirmation required",
                "result_label": "Plan ready ⏳",
            },
        ],
    )

    assert "> [!IMPORTANT]" in body
    assert "fresh plan" in body


def test_unrelated_run_status_marker_is_protected(monkeypatch) -> None:
    run_id = _PR14_RUN_ID
    other_run_id = "1788136030794.faf33c46"
    comments = {
        100: bound_comment(_production_progress_body(run_id=run_id)),
        101: bound_comment(_production_progress_body(run_id=other_run_id)),
    }
    client = _MemoryGitHubClient(comments)
    render_comments._delete_transient_status_comment(
        client, "williaumwu/openci-test-gitops", 14, run_id
    )
    assert client.deleted == [100]
    assert 101 in comments


def test_repeated_terminal_cleanup_is_idempotent() -> None:
    comments = {200: bound_comment(_production_progress_body())}
    client = _MemoryGitHubClient(comments)
    render_comments._delete_transient_status_comment(
        client, "williaumwu/openci-test-gitops", 14, _PR14_RUN_ID
    )
    render_comments._delete_transient_status_comment(
        client, "williaumwu/openci-test-gitops", 14, _PR14_RUN_ID
    )
    assert client.deleted == [200]


def test_terminal_failure_cleanup_still_deletes_progress(monkeypatch) -> None:
    comments = {300: bound_comment(_production_progress_body())}
    client = _MemoryGitHubClient(comments)
    monkeypatch.setattr(render_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render_handler, "GitHubClient", lambda _: client)
    monkeypatch.setattr(render_handler, "_delete_and_repost_unmanaged", lambda *_args: None)
    monkeypatch.setattr(render_handler, "_cleanup_terminal_mutation_comments", lambda *_args: [])

    result = render_handler._render_pipeline_failure(
        {
            "run_id": _PR14_RUN_ID,
            "action": "apply",
            "webhook_info": {
                "repo_name": "williaumwu/openci-test-gitops",
                "pr_number": 14,
                "commit_hash": _PR14_COMMIT,
            },
            "settings": {"ssm_openci_tf_github_token": "/token"},
            "pipeline_failure": {
                "failed_step": "infra/vpc",
                "action": "apply",
            },
        }
    )

    assert result["pipeline_failure_rendered"] is True
    assert client.deleted == [300]


def test_cleanup_delete_failure_fails_loud() -> None:
    class Client(_MemoryGitHubClient):
        def delete_comment(self, _repo, comment_id: int) -> None:
            raise requests.HTTPError("github delete failed")

    comments = {400: bound_comment(_production_progress_body())}
    client = Client(comments)
    with pytest.raises(requests.HTTPError, match="github delete failed"):
        render_comments._delete_transient_status_comment(
            client, "williaumwu/openci-test-gitops", 14, _PR14_RUN_ID
        )


def test_ensure_trailing_status_comment_marker_recenters_buried_marker() -> None:
    buried = _pr14_buried_marker_body()
    fixed = ensure_trailing_status_comment_marker(buried, _PR14_RUN_ID)
    prefix = status_comment_marker_prefix(_PR14_RUN_ID)
    assert body_has_status_comment_marker_prefix(fixed, prefix)
