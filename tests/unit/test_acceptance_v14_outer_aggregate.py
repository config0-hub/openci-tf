# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production-shaped tests for acceptance-v14 Unicode maxima and failed-child schema fixes."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.domain.engine.artifact_limits import (
    MAX_OUTER_CHILD_ERROR_CHARS,
    MAX_OUTER_CHILD_ERROR_JSON_BYTES,
    MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES,
    MAX_OUTER_MAP_OUTCOME_BYTES,
    MAX_OUTER_POST_MAP_STATE_BYTES,
    STEP_FUNCTIONS_STATE_LIMIT,
)
from src.domain.engine.deployment_buckets import maximum_foundation_bucket_names
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.inner_state import serialized_state_bytes
from src.domain.engine.manifest import (
    _canonical_manifest_digest,
    build_failure_manifest,
)
from src.domain.engine.outer_map_state import (
    build_compact_resolve_result,
    validate_outer_resolve_result,
)
from src.domain.engine.artifact_paths import build_folder_artifact_keys, manifest_key
from src.domain.engine.plan_artifacts import expected_plan_artifact_uris
from src.domain.engine.result import ExecutionResult
from src.domain.engine.summary import (
    bounded_error_text,
    bounded_summary,
    build_outer_map_outcome,
)
from src.domain.run.folder_id import decode_folder_id, encode_folder_id
from src.services.run_folder import collect, write_failure_manifest
from tests.unit.manifest_fixtures import complete_plan_object_mocks
from tests.unit.test_acceptance_v10_outer_aggregate import (
    _full_map_item,
    _handler_event,
    _maximum_folder_config,
    _valid_step,
)
from tests.unit.test_acceptance_v13_outer_aggregate import (
    _max_folder,
    _max_repo_name,
    _production_child_success_output,
)


def _max_escape_unicode_folder(index: int) -> str:
    """Distinct printable NFC folders maximizing JSON escapes per accepted UTF-8 byte."""
    folder = ("¡" * 95) + chr(0x0100 + index)
    assert len(folder) == 96
    assert len(folder.encode("utf-8")) == 192
    return folder


def _fifty_maximum_unicode_items() -> list[dict]:
    folders = [_max_escape_unicode_folder(index) for index in range(50)]
    repo = _max_repo_name()
    config = _maximum_folder_config()
    items: list[dict] = []
    for folder in folders:
        item = _full_map_item(folder, config=config)
        item["repo_name"] = repo
        item["folder"] = folder
        items.append(item)
    return items


def _production_child_failure_output(item: dict, *, tmp_bucket: str, done_bucket: str, error: str) -> dict:
    folder = str(item["folder"])
    attempt = int(item.get("attempt") or 0)
    exec_id = str(item["execution_id"])
    repo_name = str(item.get("repo_name") or _max_repo_name())
    run_id = str(item["run_id"])
    folder_keys = build_folder_artifact_keys(repo_name=repo_name, run_id=run_id, folder_path=folder)
    plan_metadata = expected_plan_artifact_uris(
        bucket=tmp_bucket,
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
    ).metadata
    pointers = {
        "artifacts_prefix": f"s3://{tmp_bucket}/{folder_keys.prefix}",
        "done": f"s3://{done_bucket}/{exec_id}/done",
        "plan_metadata": plan_metadata,
    }
    summary = bounded_summary(
        ExecutionResult(exec_id, False, [], error), pointers, attempt=attempt
    )
    summary["manifest_s3_uri"] = f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}"
    summary["manifest_sha256"] = "a" * 64
    return summary


def test_max_escape_unicode_folder_ids_round_trip():
    for index in range(50):
        folder = _max_escape_unicode_folder(index)
        folder_id = encode_folder_id(folder)
        assert decode_folder_id(folder_id) == folder
        assert len(json.dumps(folder)) > len(folder)


@pytest.mark.parametrize("folder", ["infra/\x00bad", "infra/\x1fbad", "infra/\ud800bad"])
def test_folder_ids_reject_control_and_surrogate_characters(folder):
    with pytest.raises((UnicodeEncodeError, ValueError)):
        encode_folder_id(folder)


def test_fifty_max_escape_unicode_production_shapes_fit_all_transitions():
    items = _fifty_maximum_unicode_items()[:36]
    buckets = maximum_foundation_bucket_names()
    for item in items:
        assert len(str(item["execution_id"])) == 47
    resolved = build_compact_resolve_result(
        _handler_event(), run_id="r" * 32, full_items=items, skipped=[]
    )
    outcomes = [
        build_outer_map_outcome(
            folder=str(item["folder"]),
            account_id=str(item["account_id"]),
            execution_id=str(item["execution_id"]),
            output=_production_child_success_output(
                item, tmp_bucket=buckets["tmp"], done_bucket=buckets["done"]
            ),
        )
        for item in items
    ]
    aggregate = serialized_state_bytes(outcomes)
    assert aggregate <= MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES
    post_map = {**resolved, "outcomes": outcomes}
    assert serialized_state_bytes(post_map) <= MAX_OUTER_POST_MAP_STATE_BYTES
    assert serialized_state_bytes(post_map) <= STEP_FUNCTIONS_STATE_LIMIT
    validate_outer_resolve_result(resolved)


@pytest.mark.parametrize("character", ["x", "¡", "界", " "])
def test_error_bounding_uses_compact_json_bytes(character):
    bounded = bounded_error_text(character * 2000)
    assert bounded is not None
    assert len(json.dumps(bounded, separators=(",", ":")).encode()) <= MAX_OUTER_CHILD_ERROR_JSON_BYTES
    assert len(bounded) <= MAX_OUTER_CHILD_ERROR_CHARS


def test_fifty_astral_error_failures_fit_outer_state():
    items = _fifty_maximum_unicode_items()[:36]
    buckets = maximum_foundation_bucket_names()
    outcomes = [
        build_outer_map_outcome(
            folder=str(item["folder"]),
            account_id=str(item["account_id"]),
            execution_id=str(item["execution_id"]),
            output=_production_child_failure_output(
                item,
                tmp_bucket=buckets["tmp"],
                done_bucket=buckets["done"],
                error=" " * 2000,
            ),
        )
        for item in items
    ]
    resolved = build_compact_resolve_result(
        _handler_event(), run_id="r" * 32, full_items=items, skipped=[]
    )
    assert serialized_state_bytes(outcomes) <= MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES
    assert serialized_state_bytes({**resolved, "outcomes": outcomes}) <= MAX_OUTER_POST_MAP_STATE_BYTES


def test_fifty_ascii_maxima_remain_passing():
    folders = [_max_folder(index) for index in range(50)]
    repo = _max_repo_name()
    config = _maximum_folder_config()
    items = []
    for folder in folders:
        item = _full_map_item(folder, config=config)
        item["repo_name"] = repo
        item["folder"] = folder
        items.append(item)
    resolved = build_compact_resolve_result(
        _handler_event(), run_id="r" * 32, full_items=items, skipped=[]
    )
    validate_outer_resolve_result(resolved)


@pytest.mark.parametrize("engine_error_chars", [256, 2000])
def test_collect_bounds_engine_failure_before_manifest_commit(monkeypatch, engine_error_chars):
    folder = _max_escape_unicode_folder(0)
    repo = _max_repo_name()
    run = "r" * 32
    exec_id = compose_execution_id(run, folder, 0)
    sha = "a" * 40
    buckets = maximum_foundation_bucket_names()
    tmp_bucket = buckets["tmp"]
    done_bucket = buckets["done"]
    package_bucket = buckets["package"]
    uris = expected_plan_artifact_uris(
        bucket=tmp_bucket,
        repo_name=repo,
        run_id=run,
        folder_path=folder,
    )
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id,
        repo_name=repo,
        run_id=run,
        commit_hash=sha,
        account_id="123456789012",
        folder=folder,
        attempt=0,
        last_modified=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )
    manifest = {
        "manifest_s3_uri": f"s3://{tmp_bucket}/{manifest_key(repo, run, folder)}",
        "manifest_sha256": "a" * 64,
        "failure_reason": "x" * engine_error_chars,
    }
    committed_manifest = build_failure_manifest(
        execution_id=exec_id,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        package_bucket=package_bucket,
        action="plan",
        failure_reason="x" * engine_error_chars,
        run_id=run,
        repo_name=repo,
        commit_hash=sha,
        account_id="123456789012",
        folder=folder,
        attempt=0,
        generated_at_source=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )
    committed_manifest["manifest_sha256"] = _canonical_manifest_digest(committed_manifest)
    call_order: list[str] = []
    persisted: list[dict] = []
    original_validate = collect.validate_outer_child_output

    def track_validate(*args, **kwargs):
        call_order.append("validate")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(collect, "validate_outer_child_output", track_validate)

    monkeypatch.setenv("TMP_BUCKET_NAME", tmp_bucket)
    monkeypatch.setenv("DONE_BUCKET_NAME", done_bucket)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", package_bucket)
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(collect, "build_manifest", lambda **_kwargs: manifest)
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata)
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)

    def fake_put(*_args, **_kwargs):
        call_order.append("put")
        return "v1"

    def fake_registry(**kwargs):
        call_order.append("registry")
        persisted.append(kwargs)

    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "publish_execution_pointer", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "put_json_create_only", fake_put)
    monkeypatch.setattr(collect, "put_folder_attempt", fake_registry)

    event = {
        "exec_id": exec_id,
        "attempt": 0,
        "succeeded": False,
        "credential_expired": False,
        "steps": [_valid_step(status="failed", exit_code=1, output="boom")],
        "error": "x" * engine_error_chars,
        "pointers": {
            "artifacts_prefix": f"s3://{tmp_bucket}/{build_folder_artifact_keys(repo_name=repo, run_id=run, folder_path=folder).prefix}",
            "done": f"s3://{done_bucket}/{exec_id}/done",
            "plan_metadata": uris.metadata,
        },
        "action": "plan",
        "repo_name": repo,
        "commit_hash": sha,
        "account_id": "123456789012",
        "folder": folder,
        "run_id": run,
        "submitted_at": 1_700_000_000.0,
    }
    summary = collect.handler(event, object())
    assert len(str(summary.get("error") or "")) <= MAX_OUTER_CHILD_ERROR_CHARS
    assert set(summary["pointers"]) == {"artifacts_prefix", "done", "plan_metadata"}
    outcome = build_outer_map_outcome(
        folder=folder,
        account_id="123456789012",
        execution_id=exec_id,
        output=summary,
    )
    assert serialized_state_bytes(outcome) <= MAX_OUTER_MAP_OUTCOME_BYTES
    assert call_order.index("validate") < call_order.index("put")
    assert persisted[0]["outcome"]["error"] == summary["error"]

    monkeypatch.setattr(
        collect,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    monkeypatch.setattr(
        collect, "get_bounded_json", lambda *_args, **_kwargs: committed_manifest
    )
    replayed = collect.handler(event, object())
    assert serialized_state_bytes(
        build_outer_map_outcome(
            folder=folder,
            account_id="123456789012",
            execution_id=exec_id,
            output=replayed,
        )
    ) == serialized_state_bytes(outcome)
    assert replayed["pointers"] == summary["pointers"]
    assert replayed["error"] == summary["error"]


def test_failure_writer_replays_authoritative_failed_plan_schema(monkeypatch):
    folder = _max_escape_unicode_folder(0)
    repo = _max_repo_name()
    run = "r" * 32
    exec_id = compose_execution_id(run, folder, 0)
    sha = "a" * 40
    buckets = maximum_foundation_bucket_names()
    committed = build_failure_manifest(
        execution_id=exec_id,
        tmp_bucket=buckets["tmp"],
        done_bucket=buckets["done"],
        package_bucket=buckets["package"],
        action="plan",
        failure_reason="x" * 2000,
        run_id=run,
        repo_name=repo,
        commit_hash=sha,
        account_id="123456789012",
        folder=folder,
        attempt=0,
        generated_at_source=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )
    persisted: list[dict] = []
    monkeypatch.setenv("TMP_BUCKET_NAME", buckets["tmp"])
    monkeypatch.setenv("DONE_BUCKET_NAME", buckets["done"])
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", buckets["package"])
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    monkeypatch.setattr(
        write_failure_manifest, "get_bounded_json", lambda *_args, **_kwargs: committed
    )
    monkeypatch.setattr(
        write_failure_manifest,
        "put_folder_attempt",
        lambda **kwargs: persisted.append(kwargs),
    )
    event = {
        "run_id": run,
        "folder": folder,
        "action": "plan",
        "account_id": "123456789012",
        "attempt": 0,
        "exec_id": exec_id,
        "failure_reason": "collect registry reconciliation failed",
        "credential_expired": True,
        "repo_name": repo,
        "commit_hash": sha,
        "submitted_at": 1_700_000_000.0,
    }
    first = write_failure_manifest.handler(event, object())
    second = write_failure_manifest.handler(event, object())
    assert first == second
    assert set(first["pointers"]) == {"artifacts_prefix", "done", "plan_metadata"}
    assert len(str(first["error"])) == MAX_OUTER_CHILD_ERROR_CHARS
    assert persisted[0]["outcome"] == persisted[1]["outcome"]
    assert persisted[0]["outcome"]["error"] == first["error"]
    assert persisted[0]["outcome"]["credential_expired"] is True
    assert first["credential_expired"] is True


def test_attempt_one_named_credential_expiry_is_terminal_not_retried():
    from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition

    states = load_rendered_run_folder_definition("read")["States"]
    choices = states["RouteProbeOutcome"]["Choices"]
    retry_predicates = next(
        rule["And"]
        for rule in choices
        if any(
            item.get("Variable") == "$.probe.credential_expired"
            for item in rule.get("And", [])
        )
    )
    assert any(
        item.get("Variable") == "$.probe.attempt"
        and item.get("NumericLessThan") == 1
        for item in retry_predicates
    )
    assert any(
        item.get("Variable") == "$.probe.attempt" and item.get("IsPresent") is True
        for item in retry_predicates
    )
    probe_retry_rule = next(
        rule
        for rule in choices
        if any(
            item.get("Variable") == "$.probe.credential_expired"
            for item in rule.get("And", [])
        )
    )
    assert probe_retry_rule["Next"] == "BookkeepCredentialRetry"
    terminal_predicates = next(
        rule["And"]
        for rule in choices
        if any(
            item.get("StringEquals") == "terminal" for item in rule.get("And", [])
        )
    )
    assert any(
        item.get("Variable") == "$.probe.probe_status"
        and item.get("StringEquals") == "terminal"
        for item in terminal_predicates
    )
    assert any(
        item.get("Variable") == "$.probe.probe_status"
        and item.get("IsPresent") is True
        for item in terminal_predicates
    )
    assert choices[4]["Next"] == "Collect"
    assert states["PrepareAndSubmit"]["Catch"][0]["Next"] == "RouteProbeOutcome"
    assert states["ProbeDone"]["Catch"][0]["Next"] == "RouteProbeOutcome"


def test_collect_failure_routes_raw_credential_expiry_to_manifest_writer():
    from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition

    states = load_rendered_run_folder_definition("read")["States"]
    assert states["Collect"]["Catch"][0]["Next"] == "WriteFailureManifest"
    assert "NormalizeCollectFailure" not in states


def test_collect_registry_preserves_terminal_credential_expiry(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        collect, "put_folder_attempt", lambda **kwargs: persisted.append(kwargs)
    )
    collect._persist_manifest_registry(
        run_id="r" * 32,
        folder="infra/a",
        account_id="123456789012",
        exec_id="r" * 47,
        attempt=1,
        succeeded=False,
        manifest={
            "manifest_s3_uri": "s3://tmp/execution/manifest.json",
            "manifest_sha256": "a" * 64,
        },
        error="ExpiredToken: target credentials expired",
        credential_expired=True,
    )
    assert persisted[0]["outcome"]["credential_expired"] is True


def test_maximum_failed_plan_report_child_includes_production_pointers():
    item = _fifty_maximum_unicode_items()[0]
    buckets = maximum_foundation_bucket_names()
    output = _production_child_failure_output(
        item,
        tmp_bucket=buckets["tmp"],
        done_bucket=buckets["done"],
        error="x" * MAX_OUTER_CHILD_ERROR_CHARS,
    )
    assert set(output["pointers"]) == {"artifacts_prefix", "done", "plan_metadata"}
    outcome = build_outer_map_outcome(
        folder=str(item["folder"]),
        account_id=str(item["account_id"]),
        execution_id=str(item["execution_id"]),
        output=output,
    )
    assert serialized_state_bytes(outcome) <= MAX_OUTER_MAP_OUTCOME_BYTES
