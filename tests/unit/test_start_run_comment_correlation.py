# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run meta rows record the originating GitHub command comment id.

Readers reconcile runs by exact comment id, never by timestamp windows, so
the webhook orchestration path must stamp ``command_comment_id`` on the meta
row for every command kind: plan, mutation intent, and mutation confirm.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.core.models import RepoSettings
from src.domain.run.request import NotificationTarget, RunRequest, build_run_request
from src.services.orchestration.start_run import start_run_from_request
from src.services.webhook.run_request import github_run_request

TRIGGER_ID = "trigger-1"
COMMIT = "a" * 40
COMMENT_ID = 4242


def _settings() -> RepoSettings:
    return RepoSettings(
        trigger_id=TRIGGER_ID,
        repo_name="org/repo",
        git_url="https://github.com/org/repo.git",
    )


def _info(comment_id: int | None = COMMENT_ID) -> dict[str, Any]:
    return {
        "trigger_id": TRIGGER_ID,
        "commit_hash": COMMIT,
        "pr_number": 7,
        "comment_id": comment_id,
        "event_type": "issue_comment",
        "username": "octocat",
        "comment_body": "tf plan pipeline proj1",
    }


def _plan_request() -> RunRequest:
    return github_run_request(
        _info(),
        action="plan",
        folders=[],
        all_flag=False,
        affected_flag=False,
        delivery_id="delivery-plan",
        pipeline="proj1",
        pipeline_step=None,
    )


def _intent_request() -> RunRequest:
    return github_run_request(
        _info(),
        action="apply",
        folders=[],
        all_flag=False,
        affected_flag=False,
        delivery_id="delivery-intent",
        intent_create=True,
        pipeline="proj1",
        pipeline_step=2,
    )


def _confirm_request() -> RunRequest:
    return github_run_request(
        _info(),
        action="apply",
        folders=[],
        all_flag=False,
        affected_flag=False,
        delivery_id="delivery-confirm",
        confirm_token="0a1b2c",
        intent_confirm=True,
    )


def _api_request() -> RunRequest:
    return build_run_request(
        trigger_id=TRIGGER_ID,
        commit_hash=COMMIT,
        action="plan",
        folder_mode="all",
        folders=[],
        idempotency_key="api-key-1",
        notification_target=NotificationTarget("registry"),
        ingress_source="api",
    )


def _start(request: RunRequest, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run start_run_from_request with mocked AWS; return the claimed meta row."""
    monkeypatch.setenv("STEP_FUNCTION_ARN", "arn:aws:states:us-east-1:1:stateMachine:sm")
    monkeypatch.setenv("APPLY_STEP_FUNCTION_ARN", "arn:aws:states:us-east-1:1:stateMachine:apply")
    monkeypatch.setenv("DESTROY_STEP_FUNCTION_ARN", "arn:aws:states:us-east-1:1:stateMachine:destroy")
    claim = MagicMock(side_effect=lambda *_a, **kwargs: (kwargs["run_record"]["run_id"], True))
    sfn = MagicMock()
    sfn.start_execution.return_value = {"executionArn": "arn:exec"}
    with (
        patch("src.services.orchestration.start_run.get_repo_settings", return_value=_settings()),
        patch("src.services.orchestration.start_run.claim_idempotent_run", claim),
        patch("src.services.orchestration.start_run.get_run", return_value=None),
        patch("src.services.orchestration.start_run.attach_sfn_execution_arn"),
        patch("src.services.orchestration.start_run.update_run_status"),
        patch("src.services.orchestration.start_run.boto3.client", return_value=sfn),
    ):
        run_id, created = start_run_from_request(request)
    assert created is True
    record = claim.call_args.kwargs["run_record"]
    assert record["run_id"] == run_id
    return record


def test_start_run_records_comment_id_for_plan_command(monkeypatch):
    record = _start(_plan_request(), monkeypatch)
    assert record["command_comment_id"] == COMMENT_ID
    assert record["pipeline"] == "proj1"


def test_start_run_records_comment_id_for_intent_command(monkeypatch):
    record = _start(_intent_request(), monkeypatch)
    assert record["command_comment_id"] == COMMENT_ID
    assert record["pipeline"] == "proj1"
    assert record["pipeline_step"] == 2


def test_start_run_records_comment_id_for_confirm_command(monkeypatch):
    record = _start(_confirm_request(), monkeypatch)
    assert record["command_comment_id"] == COMMENT_ID
    assert "pipeline_step" not in record


def test_start_run_omits_comment_id_without_github_comment(monkeypatch):
    # API ingress (no github_metadata) and comment-less events stay additive:
    # the attribute is simply absent, exactly like pre-existing rows.
    record = _start(_api_request(), monkeypatch)
    assert "command_comment_id" not in record
