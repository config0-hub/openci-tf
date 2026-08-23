# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Freshness and identity checks for the single-shot done-marker probe."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services.run_folder import poll_done


def _marker(trigger_id: str = "run") -> dict:
    return {
        "trigger_id": trigger_id,
        "status": "succeeded",
        "steps": [
            {
                "step_name": "step-0",
                "status": "succeeded",
                "exit_code": 0,
                "duration_seconds": 1.0,
                "output": "",
            }
        ],
    }


def _event(submitted_at: float, baseline: str | None = None) -> dict:
    return {
        "exec_id": "run",
        "budget": 5,
        "deadline_at": "2099-01-01T00:00:00Z",
        "attempt": 0,
        "submitted_at": submitted_at,
        "done_baseline_version_id": baseline,
    }


def test_probe_returns_pending_for_baseline_then_complete_on_next_invocation(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    submitted_at = 1_700_000_000.0
    stale_modified = datetime.fromtimestamp(submitted_at - 60, tz=timezone.utc)
    fresh_modified = datetime.fromtimestamp(submitted_at + 10, tz=timezone.utc)
    responses = iter(
        [
            (_marker(), {"version_id": "stale-v1", "last_modified": stale_modified}),
            (_marker(), {"version_id": "fresh-v2", "last_modified": fresh_modified}),
        ]
    )
    monkeypatch.setattr(
        poll_done, "get_bounded_json_with_meta", lambda *_args: next(responses)
    )

    pending = poll_done.handler(_event(submitted_at, "stale-v1"), object())
    complete = poll_done.handler(_event(submitted_at, "stale-v1"), object())

    assert pending["probe_status"] == "pending"
    assert "stale-v1" in pending["pending_reason"]
    assert complete["probe_status"] == "complete"
    assert complete["succeeded"] is True


def test_probe_returns_pending_for_old_last_modified(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    submitted_at = 1_700_000_000.0
    stale_modified = datetime.fromtimestamp(submitted_at - 60, tz=timezone.utc)
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_args: (_marker(), {"version_id": None, "last_modified": stale_modified}),
    )

    result = poll_done.handler(_event(submitted_at), object())

    assert result["probe_status"] == "pending"
    assert "last_modified" in result["pending_reason"]


def test_probe_falls_back_to_submitted_at_plus_legacy_budget(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setattr(
        poll_done, "get_bounded_json_with_meta", lambda *_args: (None, None)
    )
    event = {
        "exec_id": "run",
        "budget": 5,
        "attempt": 0,
        "submitted_at": 1_700_000_000.0,
        "done_baseline_version_id": None,
    }
    monkeypatch.setattr(poll_done.time, "time", lambda: 1_700_000_004.0)
    assert poll_done.handler(event, object())["probe_status"] == "pending"
    monkeypatch.setattr(poll_done.time, "time", lambda: 1_700_000_005.0)
    assert poll_done.handler(event, object())["probe_status"] == "expired"


def test_probe_honors_nested_execution_deadline_without_legacy_budget(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setattr(
        poll_done, "get_bounded_json_with_meta", lambda *_args: (None, None)
    )
    event = {
        "result": {
            "exec_id": "run",
            "attempt": 0,
            "submitted_at": 1_700_000_000.0,
        },
        "execution_context": {"deadline_at": "2099-01-01T00:00:00Z"},
    }

    assert poll_done.handler(event, object())["probe_status"] == "pending"


@pytest.mark.parametrize("deadline", [float("inf"), float("-inf"), float("nan")])
def test_probe_rejects_non_finite_deadline(deadline):
    with pytest.raises(ValueError, match="deadline_at must be finite"):
        poll_done.ProbeInput.from_event(
            {
                "exec_id": "run",
                "attempt": 0,
                "submitted_at": 1_700_000_000.0,
                "deadline_at": deadline,
            }
        )


def test_probe_reuses_build_id_from_previous_probe(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("ENGINE_CODEBUILD_PROJECT_NAME", "worker")
    monkeypatch.setattr(
        poll_done, "get_bounded_json_with_meta", lambda *_args: (None, None)
    )
    resolver_calls = []
    monkeypatch.setattr(
        poll_done.engine,
        "resolve_codebuild_build_id",
        lambda *_args, **_kwargs: resolver_calls.append(True),
    )
    event = {
        "result": {
            "exec_id": "run",
            "attempt": 0,
            "submitted_at": 1_700_000_000.0,
        },
        "probe": {"exec_id": "run", "codebuild_build_id": "build-1"},
        "deadline_at": "2099-01-01T00:00:00Z",
    }

    result = poll_done.handler(event, object())

    assert result["codebuild_build_id"] == "build-1"
    assert resolver_calls == []


def test_probe_returns_expired_without_reading_s3(monkeypatch):
    read = []
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_args: read.append(True),
    )
    result = poll_done.handler(
        {
            **_event(1_700_000_000.0),
            "deadline_at": "2000-01-01T00:00:00Z",
        },
        object(),
    )
    assert result["probe_status"] == "expired"
    assert "deadline exceeded" in result["failure_reason"]
    assert read == []


def test_probe_returns_pending_for_trigger_mismatch_then_complete(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    submitted_at = 1_700_000_000.0
    fresh_modified = datetime.fromtimestamp(submitted_at + 2, tz=timezone.utc)
    markers = iter(
        [
            (_marker("other"), {"version_id": "v1", "last_modified": fresh_modified}),
            (_marker(), {"version_id": "v2", "last_modified": fresh_modified}),
        ]
    )
    monkeypatch.setattr(
        poll_done, "get_bounded_json_with_meta", lambda *_args: next(markers)
    )

    pending = poll_done.handler(_event(submitted_at), object())
    complete = poll_done.handler(_event(submitted_at), object())

    assert pending["probe_status"] == "pending"
    assert pending["pending_reason"] == "trigger_mismatch"
    assert complete["probe_status"] == "complete"
