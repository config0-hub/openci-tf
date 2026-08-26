# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render command-context prefix and terminal comment cleanup tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from src.services.render import handler as render
from src.services.render.comments import _with_command_context

_FULL_SHA = "a" * 40
_CLONE_TOKEN = "/openci-tf/github-token"


def _plan_event(**overrides) -> dict:
    event = {
        "action": "plan",
        "folders": ["infra/a"],
        "all_flag": False,
        "affected_flag": False,
        "webhook_info": {
            "repo_name": "org/repo",
            "pr_number": 7,
            "commit_hash": _FULL_SHA,
            "trigger_id": "trigger",
            "event_type": "issue_comment",
            "comment_id": 42,
            "comment_body": "tf plan infra/a",
        },
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "outcomes": [
            {
                "folder": "infra/a",
                "account_id": "123456789012",
                "execution_id": "run.abc.0",
                "succeeded": True,
            }
        ],
        "skipped": [],
        "run_id": "1700000000000.deadbeef",
    }
    event.update(overrides)
    return event


def test_with_command_context_prefixes_body_for_github_pr():
    body = _with_command_context(_plan_event(), "## status", run_id="run-1")
    assert body.startswith("### openci-tf command")
    assert "---" in body
    assert "## status" in body


def test_terminal_apply_deletes_request_intent_and_confirm_comments(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])

    deleted_batches: list[list[int | None]] = []
    swept_tokens: list[str | None] = []

    class Client:
        def find_comments_by_tag(self, *_args):
            return []

        def delete_comment(self, *_args):
            return None

        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, _repo, _pr, needle):
            assert "confirm deadbeef" in needle
            return [(99, "openci-bot")]

    monkeypatch.setattr(
        render,
        "delete_acknowledged_command_comments",
        lambda _client, _repo, comment_ids: deleted_batches.append(list(comment_ids)) or [],
    )
    monkeypatch.setattr(
        render,
        "delete_stale_confirm_token_comments",
        lambda _client, _repo, _pr, token, **kwargs: swept_tokens.append(token) or [],
    )
    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())

    event = _plan_event(
        action="apply",
        requested_comment_id=10,
        requested_comment_body="tf apply infra/a",
        intent_comment_id=11,
        consumed_confirm_token="deadbeef",
        webhook_info={
            "repo_name": "org/repo",
            "pr_number": 7,
            "commit_hash": _FULL_SHA,
            "trigger_id": "trigger",
            "comment_id": 55,
            "comment_body": "tf apply confirm deadbeef",
        },
        outcomes=[
            {
                "folder": "infra/a",
                "account_id": "123456789012",
                "execution_id": "run.abc.0",
                "succeeded": True,
            }
        ],
    )

    result = render.handler(event, None)

    assert result["rendered"] is True
    assert deleted_batches == [[55, 10, 11]]
    assert swept_tokens == ["deadbeef"]


def test_render_cleanup_warning_surfaces_on_result(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)

    class Client:
        def find_comments_by_tag(self, *_args):
            return [1]

        def delete_comment(self, _repo, comment_id):
            if comment_id == 1:
                raise requests.RequestException("github delete failed")

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())

    result = render.handler(_plan_event(), None)

    assert result["rendered"] is True
    assert result["comment_cleanup_warnings"]
    assert "github delete failed" in result["comment_cleanup_warnings"][0]


def test_render_cleanup_404_is_not_an_error(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])

    class Client:
        def find_comments_by_tag(self, *_args):
            return []

        def delete_comment(self, *_args):
            response = SimpleNamespace(status_code=404)
            raise requests.HTTPError(response=response)

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())

    result = render.handler(_plan_event(), None)

    assert result["rendered"] is True
    assert "comment_cleanup_warnings" not in result


@pytest.mark.parametrize("flag", ["early_placeholder", "placeholder", "pipeline_failure"])
def test_render_paths_prefix_command_context(flag, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    captured: list[str] = []

    class Client:
        def create_comment(self, _repo, _pr, body):
            captured.append(body)
            return 1

        def find_comments_by_tag(self, *_args):
            return []

        def delete_comment(self, *_args):
            return None

        def delete_and_repost(self, _repo, _pr, body, _tag):
            captured.append(body)
            return 1

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args, **kwargs: captured.append(_args[3]) or 1)
    monkeypatch.setattr(render, "_delete_and_repost_unmanaged", lambda *_args: captured.append(_args[3]) or 1)
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)

    base = _plan_event()
    if flag == "early_placeholder":
        render.handler(
            {
                **base,
                "early_placeholder": True,
                "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
            },
            None,
        )
    elif flag == "placeholder":
        render.handler(
            {
                **base,
                "placeholder": True,
                "map_items": [{"folder": "infra/a", "account_id": "123456789012"}],
            },
            None,
        )
    else:
        render.handler(
            {
                **base,
                "pipeline_failure": {"failed_step": "ValidateAndResolve"},
                "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
            },
            None,
        )

    assert captured
    assert any("### openci-tf command" in body for body in captured)
