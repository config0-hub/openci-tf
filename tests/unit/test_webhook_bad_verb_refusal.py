# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Webhook refusal comments for unknown tf verbs."""

from __future__ import annotations

import json

from src.core.models import RepoSettings
from src.services.webhook import handler as webhook

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
    posted: list[tuple[str, int, str]] = []

    def fake_post(webhook_info, settings, body):
        posted.append((webhook_info["repo_name"], webhook_info["pr_number"], body))

    monkeypatch.setattr(webhook, "post_pr_comment", fake_post)
    monkeypatch.setattr(webhook, "get_repo_settings", lambda _: SETTINGS)
    monkeypatch.setattr(webhook, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        webhook,
        "get_pull_request",
        lambda *_: {
            "head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}},
            "base": {"repo": {"full_name": "org/repo"}},
        },
    )
    monkeypatch.setattr(webhook, "get_collaborator_permission", lambda *_: "write")
    monkeypatch.setattr(
        webhook,
        "start_run_from_request",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not start run")),
    )
    return posted


def test_webhook_posts_refusal_for_unknown_tf_verb(monkeypatch):
    posted = _wire_webhook(monkeypatch)
    command = "tf frobnicate terraform/primary/ap-northeast-1/04-cloudwatch-log-group"

    response = webhook.handler(_event(command), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"message": "Event ignored"}
    assert len(posted) == 1
    repo, pr_number, body = posted[0]
    assert repo == "org/repo"
    assert pr_number == 7
    assert "## tf frobnicate refused" in body
    assert "Unknown verb `frobnicate`" in body
    assert "Accepted verbs: apply, destroy, plan, report" in body


def test_webhook_silently_ignores_non_tf_comment(monkeypatch):
    posted = _wire_webhook(monkeypatch)

    response = webhook.handler(_event("please run a plan when you can"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"message": "Event ignored"}
    assert posted == []


def test_webhook_silently_ignores_known_verb_with_invalid_syntax(monkeypatch):
    posted = _wire_webhook(monkeypatch)

    response = webhook.handler(_event("tf plan infra/vpc extra-arg"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"message": "Event ignored"}
    assert posted == []
