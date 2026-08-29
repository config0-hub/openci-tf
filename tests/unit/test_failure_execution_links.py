# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic Step Functions links in terminal single-folder failure comments."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.domain.formatters.artifacts import folder_comment
from src.domain.formatters.console_urls import step_functions_execution_url
from src.domain.github.comment_object_id import format_comment_object_marker
from src.services.render import handler as render

_ACCOUNT = "123456789012"
_FULL_SHA = "a" * 40
_REGION = "us-east-1"
_EXECUTION_ARN = (
    "arn:aws:states:us-east-1:123456789012:execution:openci-tf-engine:run-1"
)
_REPO = "org/repo"
_PR = 7
_RUN_ID = "1756419360000.1a2b3c4d"


def _console_url() -> str:
    return step_functions_execution_url(_EXECUTION_ARN, region=_REGION)


def _outcome(**overrides):
    base = {
        "folder": "infra/a",
        "account_id": _ACCOUNT,
        "status": "infrastructure_error",
        "succeeded": False,
        "error": "engine failed",
        "execution_id": "run.abc.0",
    }
    base.update(overrides)
    return base


def _event(action: str, outcome: dict, **overrides) -> dict:
    event = {
        "action": action,
        "folders": ["infra/a"],
        "all_flag": action == "report",
        "affected_flag": False,
        "execution_arn": _EXECUTION_ARN,
        "webhook_info": {
            "repo_name": _REPO,
            "pr_number": _PR,
            "commit_hash": _FULL_SHA,
            "trigger_id": "trigger",
            "event_type": "issue_comment",
            "comment_id": 42,
            "comment_body": f"tf {action} infra/a",
        },
        "settings": {"ssm_openci_tf_github_token": "/openci-tf/github-token"},
        "outcomes": [outcome],
        "skipped": [],
        "run_id": _RUN_ID,
    }
    event.update(overrides)
    return event


@pytest.fixture
def posted_bodies(monkeypatch):
    """Render-handler seam: patch platform calls and capture posted comment bodies."""
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object())
    )
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_publish_report_all_pointer", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_: [])
    monkeypatch.setattr(render, "_update_run_registry", lambda *_, **__: None)
    monkeypatch.setattr(
        render,
        "GitHubClient",
        lambda _: SimpleNamespace(delete_comment=lambda *_, **__: None),
    )
    posted: list[dict] = []

    def capture(client, repo, pr, body, action, folder, **kwargs):
        posted.append(
            {
                "action": action,
                "folder": folder,
                "body": body,
                "report_all": kwargs.get("report_all", False),
            }
        )
        return 100 + len(posted)

    monkeypatch.setattr(render, "_delete_and_repost", capture)
    return posted


def _folder_post(posted: list[dict]) -> dict:
    folder_posts = [item for item in posted if item["folder"] == "infra/a"]
    assert len(folder_posts) == 1
    return folder_posts[0]


def test_failed_single_folder_plan_seam_includes_step_functions_link(posted_bodies):
    result = render.handler(_event("plan", _outcome()), None)

    assert result["rendered"] is True
    body = _folder_post(posted_bodies)["body"]
    assert f"[Step Functions execution]({_console_url()})" in body
    assert "CI Details" not in body


def test_failed_single_folder_drift_seam_includes_step_functions_link(posted_bodies):
    result = render.handler(_event("drift", _outcome()), None)

    assert result["rendered"] is True
    body = _folder_post(posted_bodies)["body"]
    assert f"[Step Functions execution]({_console_url()})" in body
    assert "CI Details" not in body


def test_failed_single_folder_report_seam_includes_execution_child(posted_bodies):
    result = render.handler(_event("report", _outcome(status="failed")), None)

    assert result["rendered"] is True
    body = _folder_post(posted_bodies)["body"]
    assert "> <summary>Execution</summary>" in body
    assert f"[Step Functions execution]({_console_url()})" in body
    assert "CodeBuild" not in body
    assert "### openci-tf command" not in body
    assert "- run id:" not in body
    assert "- commit:" not in body
    assert "CI Details" not in body


def test_report_failure_seam_summary_has_no_execution_metadata(posted_bodies):
    render.handler(_event("report", _outcome(status="failed")), None)

    summary_posts = [item for item in posted_bodies if item["folder"] == "all"]
    assert len(summary_posts) == 1
    body = summary_posts[0]["body"]
    assert "- run id:" not in body
    assert "- commit:" not in body
    assert "CI Details" not in body
    assert "CodeBuild" not in body


def test_failed_plan_seam_marker_and_bounded_markdown_stay_intact(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object())
    )
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_: [])
    monkeypatch.setattr(render, "_update_run_registry", lambda *_, **__: None)
    created: list[str] = []
    client = SimpleNamespace(
        delete_comment=lambda *_, **__: None,
        create_comment=lambda _repo, _pr, body: created.append(body) or 1,
    )
    monkeypatch.setattr(render, "GitHubClient", lambda _: client)

    render.handler(
        _event(
            "plan",
            _outcome(error="token=engine-secret " + ("x" * 500)),
        ),
        None,
    )

    marker = format_comment_object_marker(_REPO, _PR, "plan", "infra/a")
    assert len(created) == 1
    body = created[0]
    assert body.endswith(marker)
    assert body.count(marker) == 1
    assert "engine-secret" not in body
    assert "***" in body
    assert len(re.findall(r"<details\b", body)) == body.count("</details>")
    assert body.count("```") % 2 == 0


def test_plan_failure_variants_carry_console_url():
    variants = [
        _outcome(),
        _outcome(status="failed", credential_expired=True),
        _outcome(status="failed"),
    ]
    for outcome in variants:
        rendered = folder_comment(
            "infra/a",
            outcome,
            {},
            action="plan",
            commit_hash=_FULL_SHA,
            console_url=_console_url(),
        )
        assert f"[Step Functions execution]({_console_url()})" in rendered, outcome


def test_drift_failure_variants_carry_console_url():
    variants = [
        _outcome(),
        _outcome(status="failed", credential_expired=True),
        _outcome(status="failed"),
    ]
    for outcome in variants:
        rendered = folder_comment(
            "infra/a",
            outcome,
            {},
            action="drift",
            commit_hash=_FULL_SHA,
            console_url=_console_url(),
        )
        assert f"[Step Functions execution]({_console_url()})" in rendered, outcome


def test_report_failure_variants_carry_execution_child_without_codebuild():
    variants = [
        _outcome(),
        _outcome(status="failed", credential_expired=True),
        _outcome(status="failed"),
    ]
    for outcome in variants:
        rendered = folder_comment(
            "infra/a",
            outcome,
            {},
            action="report",
            commit_hash=_FULL_SHA,
            console_url=_console_url(),
            run_id=_RUN_ID,
            repo_name=_REPO,
            pr_number=_PR,
        )
        assert "> <summary>Execution</summary>" in rendered, outcome
        assert f"[Step Functions execution]({_console_url()})" in rendered, outcome
        assert "CodeBuild" not in rendered, outcome


def test_report_failure_comment_excludes_command_run_commit_and_ci_metadata():
    rendered = folder_comment(
        "infra/a",
        _outcome(status="failed"),
        {},
        action="report",
        commit_hash=_FULL_SHA,
        console_url=_console_url(),
        run_id=_RUN_ID,
        repo_name=_REPO,
        pr_number=_PR,
    )
    assert "### openci-tf command" not in rendered
    assert "<summary>Metadata</summary>" not in rendered
    assert "- run id:" not in rendered
    assert "- commit:" not in rendered
    assert "CI Details" not in rendered
    assert "CodeBuild" not in rendered


def test_failure_comments_unchanged_without_console_url():
    for action in ("plan", "drift", "report"):
        rendered = folder_comment(
            "infra/a",
            _outcome(),
            {},
            action=action,
            commit_hash=_FULL_SHA,
            console_url=None,
        )
        assert "CI Details" not in rendered, action
        assert "> <summary>Execution</summary>" not in rendered, action


def test_multi_folder_plan_failure_keeps_folder_comment_free_of_ci_details():
    rendered = folder_comment(
        "infra/a",
        _outcome(),
        {},
        action="plan",
        commit_hash=_FULL_SHA,
        console_url=_console_url(),
    )
    assert "CI Details" not in rendered
    assert f"[Step Functions execution]({_console_url()})" in rendered


def test_report_failure_execution_child_is_separate_report_style_child():
    rendered = folder_comment(
        "infra/a",
        _outcome(status="failed"),
        {},
        action="report",
        commit_hash=_FULL_SHA,
        console_url=_console_url(),
        run_id=_RUN_ID,
        repo_name=_REPO,
        pr_number=_PR,
    )
    assert rendered.count("> <summary>Execution</summary>") == 1
    assert len(re.findall(r"<details\b", rendered)) == rendered.count("</details>")
