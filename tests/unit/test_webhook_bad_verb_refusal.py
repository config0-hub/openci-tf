# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Webhook handling for invalid tf comment grammar (replaces unknown-verb refusal)."""

from __future__ import annotations

import json

import pytest

from src.core.models import RepoSettings
from src.services.webhook import handler as webhook
from tests.helpers.fake_locks_table import FakeLocksTable

_FULL_SHA = "a" * 40
_GUID_A = "38355582-3487-2086-500a-1b2c3d4e5f60"

SETTINGS = RepoSettings(
    trigger_id="trigger",
    repo_name="org/repo",
    git_url="https://github.com/org/repo.git",
    secret="secret",
    ssm_openci_tf_github_token="/openci-tf/clone-token/test",
)


def _event(command: str, *, comment_id: int = 42) -> dict[str, object]:
    return {
        "trigger_id": "trigger",
        "headers": {
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": _GUID_A,
        },
        "body": json.dumps({
            "action": "created",
            "comment": {"id": comment_id, "body": command, "user": {"login": "alice"}},
            "issue": {"number": 7, "pull_request": {"url": "https://api.github.example/pr/7"}},
            "repository": {"full_name": "org/repo"},
        }),
    }


def _wire_webhook(monkeypatch):
    posted: list[tuple[str, str]] = []
    deleted: list[int] = []

    def fake_get_pr(*_):
        return {
            "state": "open",
            "head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}},
            "base": {"repo": {"full_name": "org/repo"}},
        }

    class FakeClient:
        def __init__(self, _token):
            pass

        def create_comment(self, repo, pr, body):
            posted.append((body, f"{repo}#{pr}"))
            return 9000 + len(posted)

        def delete_comment(self, _repo, comment_id):
            deleted.append(comment_id)

        def token_login(self):
            return "openci-bot"

        def find_comment_by_tag(self, *_args, **_kwargs):
            return None

        def find_comments_by_tag(self, *_args, **_kwargs):
            return []

        def find_comments_by_body_substring(self, *_args, **_kwargs):
            return []

        def get_comment_body(self, *_args, **_kwargs):
            return None

        def update_comment(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(webhook, "GitHubClient", FakeClient)
    monkeypatch.setattr(webhook, "locks_table", FakeLocksTable)
    monkeypatch.setattr(webhook, "get_repo_settings", lambda _: SETTINGS)
    monkeypatch.setattr(webhook, "get_github_token", lambda _: "token")
    monkeypatch.setattr(webhook, "get_pull_request", fake_get_pr)
    monkeypatch.setattr(webhook, "get_collaborator_permission", lambda *_: "write")
    monkeypatch.setattr(webhook.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        webhook,
        "start_run_from_request",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not start run")),
    )
    return posted, deleted


def test_webhook_invalid_unknown_tf_verb_gets_audit_and_transient_help(monkeypatch):
    posted, deleted = _wire_webhook(monkeypatch)
    command = "tf frobnicate terraform/primary/ap-northeast-1/04-cloudwatch-log-group"

    response = webhook.handler(_event(command), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["message"] == "Event ignored"
    assert body["reason"] == "invalid_command"
    audit_body = next(text for text, _ in posted if "## openci-tf commands" in text)
    assert "| `tf frobnicate terraform/primary/ap-northeast-1/04-cloudwatch-log-group` | not supported |" in audit_body
    help_body = next(text for text, _ in posted if "command not accepted" in text)
    assert help_body.startswith("## openci-tf: command not accepted")
    assert 42 in deleted


def test_webhook_silently_ignores_non_tf_comment(monkeypatch):
    posted, deleted = _wire_webhook(monkeypatch)

    response = webhook.handler(_event("please run a plan when you can"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"message": "Event ignored"}
    assert posted == []
    assert deleted == []


@pytest.mark.parametrize(
    "command",
    [
        "tf plan infra/vpc extra-arg",
        "tf plan",
    ],
)
def test_webhook_invalid_tf_syntax_gets_audit_not_refusal(monkeypatch, command):
    posted, deleted = _wire_webhook(monkeypatch)

    response = webhook.handler(_event(command), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["reason"] == "invalid_command"
    assert any("## openci-tf commands" in text for text, _ in posted)
    assert not any("refused" in text for text, _ in posted)
    assert 42 in deleted
