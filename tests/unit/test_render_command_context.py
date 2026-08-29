# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render command-context prefix and terminal comment cleanup tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests
from botocore.exceptions import ClientError

from src.domain.formatters.artifacts import status_comment_marker
from src.domain.formatters.command_audit import append_audit_row
from src.domain.github.comment_object_id import format_comment_object_marker
from src.services.render import handler as render
from src.services.render.comments import _delete_generated_comment, _with_command_context

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


def _report_event(**overrides) -> dict:
    event = _plan_event(
        action="report",
        all_flag=True,
        webhook_info={
            "repo_name": "org/repo",
            "pr_number": 7,
            "commit_hash": _FULL_SHA,
            "trigger_id": "trigger",
            "event_type": "issue_comment",
            "comment_id": 42,
            "comment_body": "tf report",
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
    event.update(overrides)
    return event


def _assert_report_body_omits_command_metadata(body: str) -> None:
    assert "### openci-tf command" not in body
    assert "- command: `tf report`" not in body
    assert "triggering comment" not in body.lower()
    assert "- run id:" not in body
    assert "- commit:" not in body
    assert "\n\n---\n\n" not in body


def test_with_command_context_is_noop_for_report_action():
    body = _with_command_context(_report_event(), "**Type:** Report\n\nreport body", run_id="run-1")
    assert body == "**Type:** Report\n\nreport body"


def _noop_github_client():
    return SimpleNamespace(delete_comment=lambda *_, **__: None)


def test_report_terminal_render_omits_command_metadata(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "list_prefix_object_names", lambda *_: frozenset())
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_github_client())
    monkeypatch.setattr(render, "_publish_report_all_pointer", lambda *_, **__: None)
    posted: list[tuple[str, str, str, bool, bool]] = []

    def capture(*args, **kwargs):
        posted.append(
            (args[3], args[4], args[5], kwargs.get("report_all", False), kwargs.get("emit_marker", True))
        )
        return 1

    monkeypatch.setattr(render, "_delete_and_repost", capture)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])

    result = render.handler(_report_event(), None)

    assert result["rendered"] is True
    assert len(posted) == 2
    folder_body, folder_action, folder_name, folder_report_all, folder_emit = posted[0]
    summary_body, summary_action, summary_name, summary_report_all, summary_emit = posted[1]
    assert folder_action == "report" and folder_name == "infra/a" and not folder_report_all
    assert summary_action == "report" and summary_name == "all" and summary_report_all
    assert folder_emit is True and summary_emit is True
    for body in (folder_body, summary_body):
        _assert_report_body_omits_command_metadata(body)
    assert "**Type:** Report" in summary_body
    repo, pr = "org/repo", 7
    folder_marker = format_comment_object_marker(repo, pr, "report", "infra/a")
    summary_marker = format_comment_object_marker(repo, pr, "report-all", "all")
    assert render._managed_comment_marker(repo, pr, "report", "infra/a") == folder_marker
    assert render._managed_comment_marker(repo, pr, "report", "all", report_all=True) == summary_marker


def test_report_placeholder_omits_command_metadata(monkeypatch):
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_github_client())
    posted: list[str] = []
    monkeypatch.setattr(
        render,
        "_delete_and_repost",
        lambda *_args, **kwargs: posted.append(_args[3]) or 1,
    )

    result = render.handler(
        {
            **_report_event(),
            "placeholder": True,
            "map_items": [{"folder": "infra/a", "account_id": "123456789012"}],
            "skipped": [],
        },
        None,
    )

    assert result["placeholder_rendered"] is True
    assert len(posted) == 2
    for body in posted:
        _assert_report_body_omits_command_metadata(body)
    assert "## openci-tf report" in posted[1]


def test_plan_render_still_prefixes_command_metadata(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_github_client())
    posted: list[str] = []
    monkeypatch.setattr(
        render,
        "_delete_and_repost",
        lambda *_args, **kwargs: posted.append(_args[3]) or 1,
    )
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])

    render.handler(_plan_event(), None)

    assert posted
    assert any("### openci-tf command" in body for body in posted)
    assert any("- command: `tf plan infra/a`" in body for body in posted)


def test_generated_marker_cleanup_deletes_only_bot_comments():
    marker_deleted: list[int] = []
    marker = format_comment_object_marker("org/repo", 7, "plan", "infra/a")
    bodies = {
        100: f"## plan\n\n{marker}",
        101: f"## human quote\n\n{marker}",
    }

    class Client:
        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, _repo, _pr, needle):
            if "comment_object_id:" in needle:
                return [(100, "openci-bot"), (101, "alice")]
            return []

        def get_comment_body(self, _repo, comment_id):
            return bodies[comment_id]

        def delete_comment(self, _repo, comment_id):
            marker_deleted.append(comment_id)

    _delete_generated_comment(Client(), "org/repo", 7, "plan", "infra/a")

    assert marker_deleted == [100]


def test_generated_marker_cleanup_does_not_delete_poisoned_audit_comment():
    marker_deleted: list[int] = []
    marker = format_comment_object_marker("org/repo", 7, "plan", "infra/a")
    audit_body = append_audit_row(
        None,
        command_text="tf banana",
        status="not supported",
        repo_name="org/repo",
        pr_number=7,
        delivery_id="delivery-1",
    ).replace("tf banana", f"tf banana {marker}")
    bodies = {100: audit_body, 101: f"## plan\n\n{marker}"}

    class Client:
        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, _repo, _pr, needle):
            if needle == marker:
                return [(comment_id, "openci-bot") for comment_id in bodies]
            return []

        def get_comment_body(self, _repo, comment_id):
            return bodies[comment_id]

        def delete_comment(self, _repo, comment_id):
            marker_deleted.append(comment_id)

    _delete_generated_comment(Client(), "org/repo", 7, "plan", "infra/a")

    assert marker_deleted == [101]


def test_terminal_apply_recovers_metadata_and_deletes_request_intent_and_confirm_comments(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render,
        "get_intent_record",
        lambda token: {
            "token": token,
            "trigger_id": "trigger",
            "pr_number": 7,
            "action": "apply",
            "requested_comment_id": 10,
            "requested_comment_body": "tf apply infra/a",
            "intent_comment_id": 11,
        },
    )
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
        requested_comment_id=None,
        requested_comment_body=None,
        intent_comment_id=None,
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


def test_terminal_apply_recovery_ignores_foreign_pr_token_and_deletes_current_confirm_only(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render,
        "get_intent_record",
        lambda token: {
            "token": token,
            "trigger_id": "trigger-one",
            "pr_number": 1,
            "action": "apply",
            "requested_comment_id": 10,
            "requested_comment_body": "tf apply infra/a",
            "intent_comment_id": 11,
        },
    )
    deleted_ids: list[int] = []

    class Client:
        pass

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(render, "_delete_and_repost_unmanaged", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(
        render,
        "delete_acknowledged_command_comments",
        lambda _client, _repo, comment_ids: deleted_ids.extend(
            comment_id for comment_id in comment_ids if isinstance(comment_id, int)
        )
        or [],
    )
    monkeypatch.setattr(render, "delete_stale_confirm_token_comments", lambda *_args, **_kwargs: [])

    result = render.handler(
        _plan_event(
            action="apply",
            confirm_token="deadbeef",
            webhook_info={
                "repo_name": "org/repo",
                "pr_number": 2,
                "commit_hash": _FULL_SHA,
                "trigger_id": "trigger-two",
                "comment_id": 55,
                "comment_body": "tf apply confirm deadbeef",
            },
            pipeline_failure={"failed_step": "ConfirmApplyIntent"},
            execution_arn="arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        ),
        None,
    )

    assert result["pipeline_failure_rendered"] is True
    assert deleted_ids == [55]


def test_terminal_destroy_recovery_ignores_apply_token_and_deletes_current_confirm_only(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render,
        "get_intent_record",
        lambda token: {
            "token": token,
            "trigger_id": "trigger",
            "pr_number": 7,
            "action": "apply",
            "requested_comment_id": 10,
            "requested_comment_body": "tf apply infra/a",
            "intent_comment_id": 11,
        },
    )
    deleted_ids: list[int] = []

    class Client:
        pass

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(render, "_delete_and_repost_unmanaged", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(
        render,
        "delete_acknowledged_command_comments",
        lambda _client, _repo, comment_ids: deleted_ids.extend(
            comment_id for comment_id in comment_ids if isinstance(comment_id, int)
        )
        or [],
    )
    monkeypatch.setattr(render, "delete_stale_confirm_token_comments", lambda *_args, **_kwargs: [])

    result = render.handler(
        _plan_event(
            action="destroy",
            confirm_token="deadbeef",
            webhook_info={
                "repo_name": "org/repo",
                "pr_number": 7,
                "commit_hash": _FULL_SHA,
                "trigger_id": "trigger",
                "comment_id": 55,
                "comment_body": "tf destroy confirm deadbeef",
            },
            pipeline_failure={"failed_step": "ConfirmDestroyIntent"},
            execution_arn="arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        ),
        None,
    )

    assert result["pipeline_failure_rendered"] is True
    assert deleted_ids == [55]


def test_render_cleanup_failure_raises(monkeypatch):
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
        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, *_args):
            return [(1, "openci-bot")]

        def get_comment_body(self, *_args):
            return f"status body\n\n{status_comment_marker('1700000000000.deadbeef')}"

        def delete_comment(self, _repo, comment_id):
            if comment_id == 1:
                raise requests.RequestException("github delete failed")

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())

    with pytest.raises(requests.RequestException, match="github delete failed"):
        render.handler(_plan_event(), None)


def test_terminal_mutation_cleanup_retries_then_raises(monkeypatch):
    monkeypatch.setattr(render.time, "sleep", lambda _seconds: None)
    attempts = 0

    class Client:
        pass

    def fail_cleanup_once(_client, _repo, _ids):
        nonlocal attempts
        attempts += 1
        raise requests.RequestException("delete failed")

    monkeypatch.setattr(render, "delete_acknowledged_command_comments", fail_cleanup_once)

    with pytest.raises(requests.RequestException, match="delete failed"):
        render._cleanup_terminal_mutation_comments(
            Client(),
            "org/repo",
            7,
            _plan_event(
                action="apply",
                requested_comment_id=10,
                intent_comment_id=11,
                webhook_info={"comment_id": 55, "action": "apply"},
            ),
        )

    assert attempts == 3


def test_pipeline_failure_deletes_mutation_command_comments_and_sweeps_token(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render,
        "get_intent_record",
        lambda token: {
            "token": token,
            "trigger_id": "trigger",
            "pr_number": 7,
            "action": "apply",
            "requested_comment_id": 10,
            "requested_comment_body": "tf apply infra/a",
            "intent_comment_id": 11,
        },
    )
    posted: list[str] = []
    deleted_batches: list[list[int | None]] = []
    swept: list[tuple[str, set[int]]] = []

    class Client:
        pass

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(
        render,
        "_delete_and_repost_unmanaged",
        lambda _client, _repo, _pr, body, _suffix: posted.append(body) or 1,
    )
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(
        render,
        "delete_acknowledged_command_comments",
        lambda _client, _repo, comment_ids: deleted_batches.append(list(comment_ids)) or [],
    )
    monkeypatch.setattr(
        render,
        "delete_stale_confirm_token_comments",
        lambda _client, _repo, _pr, token, **kwargs: swept.append(
            (token, set(kwargs["exclude_comment_ids"]))
        )
        or [],
    )

    result = render.handler(
        _plan_event(
            action="apply",
            confirm_token="deadbeef",
            webhook_info={
                "repo_name": "org/repo",
                "pr_number": 7,
                "commit_hash": _FULL_SHA,
                "trigger_id": "trigger",
                "comment_id": 55,
                "comment_body": "tf apply confirm <redacted>",
            },
            pipeline_failure={"failed_step": "ConfirmApplyIntent"},
            execution_arn="arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        ),
        None,
    )

    assert result["pipeline_failure_rendered"] is True
    assert deleted_batches == [[55, 10, 11]]
    assert swept == [("deadbeef", {55, 10, 11})]
    assert posted[0].startswith("### openci-tf command")
    assert "requested command: `tf apply infra/a`" in posted[0]


def test_render_pr_list_text_prefix_failure_fallback_posts_and_cleans_mutation_comments(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(
        render,
        "list_text_prefix",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")
        ),
    )
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)

    class Client:
        pass

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())

    terminal_event = _plan_event(
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
                "status": "failed",
                "succeeded": False,
            }
        ],
    )

    with pytest.raises(ClientError):
        render.handler(terminal_event, None)

    posted: list[str] = []
    deleted_batches: list[list[int | None]] = []
    swept: list[tuple[str, set[int]]] = []

    monkeypatch.setattr(
        render,
        "_delete_and_repost_unmanaged",
        lambda _client, _repo, _pr, body, _suffix: posted.append(body) or 2,
    )
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(
        render,
        "delete_acknowledged_command_comments",
        lambda _client, _repo, comment_ids: deleted_batches.append(list(comment_ids)) or [],
    )
    monkeypatch.setattr(
        render,
        "delete_stale_confirm_token_comments",
        lambda _client, _repo, _pr, token, **kwargs: swept.append(
            (token, set(kwargs["exclude_comment_ids"]))
        )
        or [],
    )

    result = render.handler(
        {
            **terminal_event,
            "pipeline_failure": {"failed_step": "RenderPR"},
            "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        },
        None,
    )

    assert result["pipeline_failure_rendered"] is True
    assert posted and "pipeline failed at RenderPR" in posted[0]
    assert deleted_batches == [[55, 10, 11]]
    assert swept == [("deadbeef", {55, 10, 11})]


def test_read_only_pipeline_failure_does_not_delete_command_comments(monkeypatch):
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    deleted_batches: list[list[int | None]] = []
    single_deletes: list[int | None] = []
    swept: list[str] = []

    class Client:
        pass

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(render, "_delete_and_repost_unmanaged", lambda *_args: 1)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(
        render,
        "delete_acknowledged_command_comments",
        lambda _client, _repo, comment_ids: deleted_batches.append(list(comment_ids)) or [],
    )
    monkeypatch.setattr(
        render,
        "delete_acknowledged_command_comment",
        lambda _client, _repo, comment_id: single_deletes.append(comment_id) or [],
    )
    monkeypatch.setattr(
        render,
        "delete_stale_confirm_token_comments",
        lambda _client, _repo, _pr, token, **_kwargs: swept.append(token) or [],
    )

    result = render.handler(
        _plan_event(pipeline_failure={"failed_step": "ValidateAndResolve"}), None
    )

    assert result["pipeline_failure_rendered"] is True
    assert deleted_batches == []
    assert single_deletes == []
    assert swept == []


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
