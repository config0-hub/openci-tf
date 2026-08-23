# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed credential-expiry bookkeeping and one-retry equivalence tests."""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.services.run_folder import persist_retry_attempt, write_failure_manifest
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition


def _state(*, action: str = "plan", attempt: int = 0) -> dict:
    state = {
        "run_id": "r" * 32,
        "folder": "infra/a",
        "action": action,
        "account_id": "123456789012",
        "attempt": attempt,
        "budget": 60,
        "folder_config": {"account_alias": "target"},
        "upstream_urls": {"tofu:1.8.0": "https://example.invalid/tofu"},
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
        "ssm_infracost_api_key": "",
        "error": {"Error": "CredentialExpiredError", "Cause": "expired"},
    }
    if action in {"apply", "destroy"}:
        state["source_plan_run_id"] = "plan-run"
        state["folder_pin"] = {"source_run_id": "plan-run", "plan_sha256": "b" * 64}
    return state


def test_bookkeeping_persists_evidence_before_returning_resubmit(monkeypatch):
    calls: list[str] = []
    captured_manifest: dict = {}

    def write_manifest(event: dict, _context: object) -> dict:
        calls.append("manifest")
        captured_manifest.update(event)
        return {
            "exec_id": "run.infra.0",
            "registry_outcome": {
                "succeeded": False,
                "credential_expired": True,
                "error": "credential expired before retry",
            },
        }

    def persist(**_kwargs: object) -> None:
        calls.append("registry")

    monkeypatch.setattr(persist_retry_attempt.write_failure_manifest, "handler", write_manifest)
    monkeypatch.setattr(persist_retry_attempt, "put_folder_attempt", persist)

    resubmit = persist_retry_attempt.handler(
        {
            "event": _state(),
            "execution_started_at": "2026-01-01T00:00:00Z",
        },
        object(),
    )

    assert calls == ["manifest", "registry"]
    assert captured_manifest["registry_only"] is True
    assert captured_manifest["credential_expired"] is True
    assert resubmit["attempt"] == 1
    assert resubmit["submitted_at"] == "2026-01-01T00:00:00Z"
    assert "error" not in resubmit


def _json_path(state: dict[str, Any], path: str) -> object:
    value: object = state
    for part in path.removeprefix("$.").split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def _matches_choice_rule(rule: dict[str, Any], state: dict[str, Any]) -> bool:
    if "And" in rule:
        return all(_matches_choice_rule(item, state) for item in rule["And"])
    path = rule["Variable"]
    if "IsPresent" in rule:
        try:
            _json_path(state, path)
        except KeyError:
            return rule["IsPresent"] is False
        return rule["IsPresent"] is True
    value = _json_path(state, path)
    if "StringEquals" in rule:
        return value == rule["StringEquals"]
    if "BooleanEquals" in rule:
        return value is rule["BooleanEquals"]
    if "NumericLessThan" in rule:
        return isinstance(value, (int, float)) and value < rule["NumericLessThan"]
    raise ValueError(f"unsupported Choice rule: {rule}")


def test_second_credential_expiry_without_probe_routes_to_failure_manifest() -> None:
    route = load_rendered_run_folder_definition("read")["States"][
        "RouteProbeOutcome"
    ]
    state = {
        **_state(attempt=1),
        "error": {
            "Error": "CredentialExpiredError",
            "Cause": "target session expired again",
        },
    }
    assert "probe" not in state

    selected = route["Default"]
    for rule in route["Choices"]:
        if _matches_choice_rule(rule, state):
            selected = rule["Next"]
            break

    assert selected == "WriteFailureManifest"


def test_terminal_named_expiry_remains_classified_after_retry() -> None:
    event = {
        **_state(attempt=1),
        "error": {
            "Error": "CredentialExpiredError",
            "Cause": "target session expired",
        },
    }

    assert persist_retry_attempt.write_failure_manifest._credential_expired(event) is True


def test_bookkeeping_refuses_a_second_retry() -> None:
    with pytest.raises(ValueError, match="only permits attempt 0"):
        persist_retry_attempt.CredentialRetry.from_event(
            {
                "event": _state(attempt=1),
                "execution_started_at": "2026-01-01T00:00:00Z",
            }
        )


def test_raw_failure_state_is_unwrapped_without_leaking_map_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests: list[dict[str, Any]] = []
    registry_writes: list[dict[str, Any]] = []
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setenv("LANE_MODE", "read")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda _bucket, _key, manifest: manifests.append(manifest),
    )
    monkeypatch.setattr(
        write_failure_manifest,
        "put_folder_attempt",
        lambda **kwargs: registry_writes.append(kwargs),
    )
    raw_state = {
        **_state(),
        "error": {"Error": "PrepareError", "Cause": "prepare failed"},
        "folder_config": {"private": "FOLDER_CONFIG_LEAK_SENTINEL"},
        "upstream_urls": {
            "tofu": "https://UPSTREAM_URLS_LEAK_SENTINEL.invalid/archive"
        },
        "ssm_openci_tf_github_token": "SSM_GITHUB_LEAK_SENTINEL",
        "ssm_infracost_api_key": "SSM_INFRACOST_LEAK_SENTINEL",
    }

    summary = write_failure_manifest.handler(
        {
            "event": raw_state,
            "execution_started_at": "2026-01-01T00:00:00Z",
        },
        object(),
    )

    assert len(manifests) == 1
    assert len(registry_writes) == 1
    encoded_outputs = [
        json.dumps(manifests[0], sort_keys=True),
        json.dumps(summary, sort_keys=True),
        json.dumps(registry_writes[0]["outcome"], sort_keys=True),
    ]
    for encoded in encoded_outputs:
        for sentinel in (
            "folder_config",
            "upstream_urls",
            "ssm_openci_tf_github_token",
            "ssm_infracost_api_key",
            "FOLDER_CONFIG_LEAK_SENTINEL",
            "UPSTREAM_URLS_LEAK_SENTINEL",
            "SSM_GITHUB_LEAK_SENTINEL",
            "SSM_INFRACOST_LEAK_SENTINEL",
        ):
            assert sentinel not in encoded
    assert manifests[0]["generated_at"] == "2026-01-01T00:00:00Z"
    assert summary["error"] == "prepare failed"


def test_unsafe_action_reason_is_synthesized_after_reject_state_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANE_MODE", "read")
    reason = write_failure_manifest._failure_reason({"action": "apply"})
    assert "action apply not allowed in read lane" in reason


@pytest.mark.parametrize("action", ["apply", "destroy"])
def test_mutation_bookkeeping_preserves_plan_pin(action: str) -> None:
    retry = persist_retry_attempt.CredentialRetry.from_event(
        {
            "event": _state(action=action),
            "execution_started_at": "2026-01-01T00:00:00Z",
        }
    )
    resubmit = retry.resubmit_state()
    assert resubmit["source_plan_run_id"] == "plan-run"
    assert resubmit["folder_pin"]["plan_sha256"] == "b" * 64


def _route_selected(state: dict[str, Any]) -> str:
    route = load_rendered_run_folder_definition("read")["States"]["RouteProbeOutcome"]
    for rule in route["Choices"]:
        if _matches_choice_rule(rule, state):
            return rule["Next"]
    return route["Default"]


def test_mid_loop_credential_expiry_retries_despite_stale_pending_probe() -> None:
    """A freshly caught CredentialExpiredError must outrank a stale pending probe."""
    state = {
        **_state(attempt=0),
        "attempt": 0,
        "probe": {"probe_status": "pending", "exec_id": "e" * 32, "attempt": 0},
        "error": {"Error": "CredentialExpiredError", "Cause": "expired mid poll"},
    }
    assert _route_selected(state) == "BookkeepCredentialRetry"


def test_stale_error_cannot_resubmit_a_completed_probe() -> None:
    """Error rules consume the error immediately, so a complete probe can never
    coexist with a live caught error; if it somehow did, the error path (which
    terminates or books exactly one retry) must still win over resubmission of
    completed work being misread as complete-collect."""
    state = {
        **_state(attempt=0),
        "attempt": 0,
        "probe": {
            "probe_status": "complete",
            "succeeded": True,
            "exec_id": "e" * 32,
            "attempt": 0,
        },
        "error": {"Error": "CredentialExpiredError", "Cause": "stale"},
    }
    # error-first ordering: the caught error is handled (bookkeeping strips it)
    # before any probe_status rule can be evaluated on the same state.
    assert _route_selected(state) == "BookkeepCredentialRetry"


def test_exhausted_mid_loop_expiry_terminates_instead_of_polling_forever() -> None:
    """At attempt 1 a caught expiry must go to the failure manifest even when a
    stale pending probe is still in state (old graphs' terminal semantics)."""
    state = {
        **_state(attempt=1),
        "attempt": 1,
        "probe": {"probe_status": "pending", "exec_id": "e" * 32, "attempt": 1},
        "error": {"Error": "CredentialExpiredError", "Cause": "expired again"},
    }
    assert _route_selected(state) == "WriteFailureManifest"


def test_error_free_probe_statuses_route_unchanged() -> None:
    for status, expected in (
        ("pending", "WaitBeforeProbe"),
        ("complete", "Collect"),
        ("terminal", "Collect"),
        ("expired", "WriteFailureManifest"),
    ):
        state = {**_state(attempt=0), "probe": {"probe_status": status}}
        state.pop("error")
        assert _route_selected(state) == expected, status
