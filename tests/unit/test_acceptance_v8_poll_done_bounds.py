"""Production-shaped tests for acceptance-v8 PollDone / Step Functions size blocker."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.core.errors import (
    ConfigValidationError,
    DoneMarkerTooLargeError,
    MalformedResultError,
)
from src.domain.config.folder_config import parse_folder_config
from src.domain.engine.artifact_limits import (
    MAX_DONE_MARKER_BYTES,
    MAX_EXTRA_FLAG_CHARS,
    MAX_EXTRA_FLAGS_COUNT,
    MAX_EXTRA_FLAGS_SERIALIZED_BYTES,
    MAX_INNER_STATE_BYTES,
    MAX_POLL_DONE_RESULT_BYTES,
    STEP_FUNCTIONS_STATE_LIMIT,
)
from src.domain.engine.inner_state import (
    apply_result_path,
    assert_post_poll_state_within_budget,
    inner_state_budget_summary,
    max_accepted_poll_result_bytes,
    post_poll_done_state,
    serialized_state_bytes,
    validate_inner_map_item,
)
from src.domain.engine.result import (
    bound_poll_done_payload,
    bound_step_metadata,
    parse_result,
)
from src.platform.aws import s3
from src.services.run_folder import collect, poll_done, write_failure_manifest
from tests.unit.manifest_fixtures import complete_plan_object_mocks


def _fresh_modified(submitted_at: float = 1_700_000_000.0) -> datetime:
    return datetime.fromtimestamp(submitted_at + 2, tz=timezone.utc)


def _valid_engine_step(**overrides: object) -> dict:
    step = {
        "step_name": "step-0",
        "status": "succeeded",
        "exit_code": 0,
        "duration_seconds": 1.0,
        "output": "",
    }
    step.update(overrides)
    return step


def _engine_marker(
    exec_id: str,
    *,
    status: str = "succeeded",
    output_size: int = 0,
    output_text: str | None = None,
    error: str | None = None,
    step_name: str = "step-0",
) -> dict:
    output = output_text if output_text is not None else ("x" * output_size)
    step = _valid_engine_step(
        step_name=step_name,
        status="failed" if status == "failed" else "succeeded",
        exit_code=1 if status == "failed" else 0,
        output=output,
    )
    marker: dict = {"trigger_id": exec_id, "status": status, "steps": [step]}
    if error is not None:
        marker["error"] = error
    return marker


def _poll_event(exec_id: str = "run", submitted_at: float = 1_700_000_000.0) -> dict:
    return {
        "exec_id": exec_id,
        "budget": 1,
        "deadline_at": "2099-01-01T00:00:00Z",
        "attempt": 0,
        "submitted_at": submitted_at,
        "done_baseline_version_id": None,
    }


def _max_valid_extra_flags_yaml() -> str:
    flags: list[str] = []
    for _ in range(MAX_EXTRA_FLAGS_COUNT):
        trial = flags + ["x" * MAX_EXTRA_FLAG_CHARS]
        if len(json.dumps(trial, separators=(",", ":")).encode()) > MAX_EXTRA_FLAGS_SERIALIZED_BYTES:
            break
        flags.append("x" * MAX_EXTRA_FLAG_CHARS)
    if not flags:
        for length in range(MAX_EXTRA_FLAG_CHARS, 0, -1):
            if len(json.dumps(["x" * length], separators=(",", ":")).encode()) <= MAX_EXTRA_FLAGS_SERIALIZED_BYTES:
                flags = ["x" * length]
                break
    if not flags:
        return ""
    return "extra_flags:\n" + "".join(f"  - '{flag}'\n" for flag in flags)


def _maximum_map_item(*, include_max_extra_flags: bool = True) -> dict:
    flag_line = ""
    if include_max_extra_flags:
        flag_line = "extra_flags:\n  - '" + ("x" * 512) + "'\n"
    config = parse_folder_config(f"account_alias: target\n{flag_line}")
    exec_id = "r" * 32 + ".0123456789ab.0"
    upstream = {
        "tofu:1.8.0": "https://example.invalid/tofu",
        "tfsec:1.28.10": "https://example.invalid/tfsec",
        "infracost:0.10.39": "https://example.invalid/infracost",
    }
    return {
        "run_id": "r" * 32,
        "folder": "infra/production",
        "account_id": "123456789012",
        "account_binding": [
            "openci-tf-executor-readonly",
            None,
            "openci-tf-0123456789abcdef",
            3600,
        ],
        "action": "plan",
        "attempt": 0,
        "budget": 3600,
        "deadline_at": "2099-01-01T00:00:00Z",
        "folder_config": asdict(config),
        "upstream_urls": upstream,
        "execution_id": exec_id,
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
    }


def _maximum_poll_result(map_item: dict) -> dict:
    exec_id = map_item["execution_id"]
    metadata = (
        f"s3://tmp/openci-tf/org/repo/run/org/repo/{map_item['commit_hash']}/"
        f"{map_item['account_id']}/{map_item['folder']}/{exec_id}/0/plan-metadata.json"
    )
    return bound_poll_done_payload(
        {
            "exec_id": exec_id,
            "attempt": 0,
            "submitted_at": 1_700_000_000.0,
            "succeeded": False,
            "error": "e" * 500,
            "credential_expired": False,
            "steps": [
                {
                    "step_name": "step-0",
                    "status": "failed",
                    "exit_code": 1,
                    "duration_seconds": 1.0,
                    "output": "",
                }
            ],
            "pointers": {
                "artifacts_prefix": f"s3://tmp/{exec_id}/",
                "done": f"s3://done/{exec_id}/done",
                "plan_metadata": metadata,
            },
        }
    )


def test_inner_state_budget_has_substantial_headroom_below_step_functions_limit():
    summary = inner_state_budget_summary()
    assert summary["step_functions_limit"] == STEP_FUNCTIONS_STATE_LIMIT
    assert summary["max_inner_state_bytes"] < STEP_FUNCTIONS_STATE_LIMIT
    assert summary["headroom_bytes"] >= 30_000


def test_apply_result_path_matches_rendered_asl_probe_semantics():
    map_item = _maximum_map_item()
    poll_result = _maximum_poll_result(map_item)
    prepared = apply_result_path(
        map_item, "$.result", {"exec_id": map_item["execution_id"], "attempt": 0}
    )
    post = apply_result_path(prepared, "$.probe", poll_result)
    assert post == post_poll_done_state(prepared, poll_result)


def test_maximum_accepted_config_and_poll_result_remain_below_inner_budget():
    map_item = _maximum_map_item(include_max_extra_flags=True)
    validate_inner_map_item(map_item)
    poll_result = _maximum_poll_result(map_item)
    post = assert_post_poll_state_within_budget(map_item, poll_result)
    assert serialized_state_bytes(post) < MAX_INNER_STATE_BYTES
    assert serialized_state_bytes(post) < STEP_FUNCTIONS_STATE_LIMIT


def test_oversized_extra_flags_rejected_before_inner_execution():
    oversized = "x" * (MAX_EXTRA_FLAGS_SERIALIZED_BYTES + 1)
    with pytest.raises(ConfigValidationError, match="extra_flags"):
        parse_folder_config(f"account_alias: target\nextra_flags:\n  - '{oversized}'\n")


def test_audit_attack_extra_flags_and_step_metadata_rejected():
    oversized_flag = "x" * 230_810
    with pytest.raises(ConfigValidationError, match="extra_flags"):
        parse_folder_config(f"account_alias: target\nextra_flags:\n  - '{oversized_flag}'\n")
    marker = _engine_marker("run", step_name="s" * 30_000)
    with pytest.raises(MalformedResultError, match="step_name"):
        parse_result(marker, "run")


def test_bound_step_metadata_strips_raw_stdout():
    steps = [
        {
            "step_name": "step-0",
            "status": "failed",
            "exit_code": 1,
            "duration_seconds": 1.0,
            "output": "x" * 300_000,
        }
    ]
    assert bound_step_metadata(steps) == [
        {"step_name": "step-0", "status": "failed", "exit_code": 1}
    ]
    assert "output" not in bound_step_metadata(steps)[0]


def test_poll_done_rejects_300kb_engine_marker_at_read(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")

    def oversized(*_args, **_kwargs):
        raise ValueError(f"JSON object exceeds {MAX_DONE_MARKER_BYTES} bytes: s3://done/run.abc.0/done")

    monkeypatch.setattr(poll_done, "get_bounded_json_with_meta", oversized)
    with pytest.raises(DoneMarkerTooLargeError, match="exceeds"):
        poll_done.handler(_poll_event("run.abc.0"), object())


def test_poll_done_rejects_multi_mib_engine_marker_at_read(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")

    def oversized(*_args, **_kwargs):
        raise ValueError(f"JSON object exceeds {MAX_DONE_MARKER_BYTES} bytes: s3://done/run.big.0/done")

    monkeypatch.setattr(poll_done, "get_bounded_json_with_meta", oversized)
    with pytest.raises(DoneMarkerTooLargeError, match="exceeds"):
        poll_done.handler(_poll_event("run.big.0"), object())


def test_poll_done_returns_bounded_result_for_large_output_within_done_limit(monkeypatch):
    submitted_at = 1_700_000_000.0
    exec_id = "run.large.0"
    marker = _engine_marker(exec_id, output_size=200_000)
    assert len(json.dumps(marker).encode()) < MAX_DONE_MARKER_BYTES
    meta = {"version_id": "v1", "last_modified": _fresh_modified(submitted_at)}
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_args, **_kwargs: (marker, meta),
    )
    result = poll_done.handler(_poll_event(exec_id, submitted_at), object())
    assert len(json.dumps(marker).encode()) > 200_000
    assert len(json.dumps(result).encode()) < MAX_POLL_DONE_RESULT_BYTES
    assert result["steps"][0].get("output") is None
    map_item = _maximum_map_item()
    map_item["execution_id"] = exec_id
    post = post_poll_done_state(map_item, result)
    assert serialized_state_bytes(post) < MAX_INNER_STATE_BYTES


def test_large_credential_expiry_output_within_bound_is_detected_before_sanitization(monkeypatch):
    submitted_at = 1_700_000_000.0
    exec_id = "run.expire.0"
    expiry_tail = "security token included in the request is expired"
    marker = _engine_marker(
        exec_id,
        status="failed",
        output_text=("plan noise\n" * 5000) + expiry_tail,
    )
    meta = {"version_id": "v1", "last_modified": _fresh_modified(submitted_at)}
    assert len(json.dumps(marker).encode()) < MAX_DONE_MARKER_BYTES
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_args, **_kwargs: (marker, meta),
    )
    result = poll_done.handler(_poll_event(exec_id, submitted_at), object())
    assert result["credential_expired"]
    assert not result["succeeded"]
    assert "output" not in result["steps"][0]


def test_marker_just_below_done_limit_is_accepted(monkeypatch):
    submitted_at = 1_700_000_000.0
    exec_id = "run.near.0"
    pad = "a" * (MAX_DONE_MARKER_BYTES - 220)
    marker = _engine_marker(exec_id, output_text=pad)
    body = json.dumps(marker, separators=(",", ":"))
    assert len(body.encode()) < MAX_DONE_MARKER_BYTES
    meta = {"version_id": "v1", "last_modified": _fresh_modified(submitted_at)}
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_args, **_kwargs: (marker, meta),
    )
    assert poll_done.handler(_poll_event(exec_id, submitted_at), object())["succeeded"]


def test_marker_above_done_limit_raises_typed_error(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")

    def oversized(*_args, **_kwargs):
        raise ValueError(f"JSON object exceeds {MAX_DONE_MARKER_BYTES} bytes: s3://done/run/done")

    monkeypatch.setattr(poll_done, "get_bounded_json_with_meta", oversized)
    with pytest.raises(DoneMarkerTooLargeError, match="exceeds"):
        poll_done.handler(_poll_event(), object())


def test_malformed_json_raises_typed_error(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")

    def malformed(*_args, **_kwargs):
        raise ValueError("malformed JSON object: s3://done/run/done")

    monkeypatch.setattr(poll_done, "get_bounded_json_with_meta", malformed)
    with pytest.raises(MalformedResultError, match="malformed JSON"):
        poll_done.handler(_poll_event(), object())


def test_declared_content_length_overflow_raises(monkeypatch):
    client = Mock()
    client.head_object.return_value = {"ContentLength": 64}
    body = Mock()
    body.read.return_value = b"{" + (b"x" * 128)
    client.get_object.return_value = {
        "Body": body,
        "LastModified": _fresh_modified(),
        "VersionId": "v1",
    }
    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: client)
    with pytest.raises(ValueError, match="declared content length"):
        s3.get_bounded_json_with_meta("done", "run/done", MAX_DONE_MARKER_BYTES)


def test_oversized_marker_routes_to_single_failure_manifest(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(write_failure_manifest, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_folder_attempt",
        lambda **kwargs: persisted.append(kwargs),
    )
    summary = write_failure_manifest.handler(
        {
            "run_id": "run123",
            "folder": "infra/a",
            "action": "plan",
            "account_id": "123456789012",
            "attempt": 0,
            "failure_reason": f"done marker exceeds {MAX_DONE_MARKER_BYTES} bytes",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "submitted_at": 1_700_000_000.0,
        },
        object(),
    )
    assert summary["succeeded"] is False
    assert len(persisted) == 1
    assert persisted[0]["manifest_sha256"] == summary["manifest_sha256"]
    assert len(json.dumps(summary).encode()) < MAX_POLL_DONE_RESULT_BYTES


def test_malformed_step_metadata_routes_to_single_failure_manifest(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(write_failure_manifest, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_folder_attempt",
        lambda **kwargs: persisted.append(kwargs),
    )
    summary = write_failure_manifest.handler(
        {
            "run_id": "run123",
            "folder": "infra/a",
            "action": "plan",
            "account_id": "123456789012",
            "attempt": 0,
            "failure_reason": "done marker step_name exceeds length limit",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "submitted_at": 1_700_000_000.0,
        },
        object(),
    )
    assert len(persisted) == 1
    assert summary["manifest_sha256"] == persisted[0]["manifest_sha256"]


def test_collect_after_bounded_poll_shape_produces_single_manifest(monkeypatch):
    submitted_at = 1_700_000_000.0
    exec_id = "run.collect.0"
    poll_result = bound_poll_done_payload(
        {
            "exec_id": exec_id,
            "attempt": 0,
            "submitted_at": submitted_at,
            "succeeded": False,
            "error": "plan failed",
            "credential_expired": False,
            "steps": [{"step_name": "step-0", "status": "failed", "exit_code": 1, "duration_seconds": 1.0, "output": ""}],
            "pointers": {
                "artifacts_prefix": f"s3://tmp/{exec_id}/",
                "done": f"s3://done/{exec_id}/done",
            },
        }
    )
    persisted: list[dict] = []
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "publish_execution_pointer", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    monkeypatch.setattr(
        collect,
        "put_folder_attempt",
        lambda **kwargs: persisted.append(kwargs),
    )
    last_modified = datetime(2026, 8, 10, tzinfo=timezone.utc)
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id,
        repo_name="org/repo",
        run_id="run",
        commit_hash="a" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        last_modified=last_modified,
    )
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata)
    monkeypatch.setattr(collect, "resolve_run_artifact_layout", lambda **_kwargs: __import__(
        "src.domain.engine.run_artifact_layout", fromlist=["RunArtifactLayout"]
    ).RunArtifactLayout(
        folder_keys=__import__(
            "src.domain.engine.artifact_paths", fromlist=["build_folder_artifact_keys"]
        ).build_folder_artifact_keys(
            repo_name="org/repo", run_id="run123", folder_path="infra/a"
        ),
        pr_number=None,
        pointer_type=None,
    ))
    summary = collect.handler(
        {
            **poll_result,
            "action": "plan",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "folder": "infra/a",
            "account_id": "123456789012",
            "run_id": "run123",
        },
        object(),
    )
    assert len(persisted) == 1
    assert summary["manifest_sha256"] == persisted[0]["manifest_sha256"]
    assert len(json.dumps(summary).encode()) < MAX_POLL_DONE_RESULT_BYTES


def test_bound_poll_done_payload_rejects_runaway_payload():
    payload = {
        "exec_id": "run",
        "attempt": 0,
        "submitted_at": 1.0,
        "succeeded": False,
        "error": "e" * (MAX_POLL_DONE_RESULT_BYTES * 2),
        "credential_expired": False,
        "steps": [{"step_name": "step-0", "status": "failed", "exit_code": 1, "duration_seconds": 1.0, "output": ""}] * 4000,
        "pointers": {"done": "s3://done/run/done", "artifacts_prefix": "s3://tmp/run/"},
    }
    with pytest.raises(ValueError, match="poll done result exceeds"):
        bound_poll_done_payload(payload)


def test_parse_result_still_reads_full_output_for_derivation():
    marker = _engine_marker("run", status="failed", output_text="Error: tofu plan exploded")
    result = parse_result(marker, "run")
    assert result.error == "Error: tofu plan exploded"
    assert result.steps[0]["output"] == "Error: tofu plan exploded"
def test_rendered_asl_probe_routes_catchable_failures_to_manifest_writer():
    from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition

    probe = load_rendered_run_folder_definition("read")["States"]["ProbeDone"]
    assert probe["ResultPath"] == "$.probe"
    assert probe["Next"] == "RouteProbeOutcome"
    assert probe["Catch"][1]["ErrorEquals"] == ["States.ALL"]
    assert probe["Catch"][1]["Next"] == "WriteFailureManifest"


def test_max_accepted_poll_result_budget_tracks_map_item_size():
    small_item = _maximum_map_item(include_max_extra_flags=False)
    large_item = _maximum_map_item(include_max_extra_flags=True)
    assert serialized_state_bytes(large_item) > serialized_state_bytes(small_item)
    remaining_large = MAX_INNER_STATE_BYTES - serialized_state_bytes(large_item)
    remaining_small = MAX_INNER_STATE_BYTES - serialized_state_bytes(small_item)
    assert remaining_large < remaining_small
    assert max_accepted_poll_result_bytes(large_item) == min(MAX_POLL_DONE_RESULT_BYTES, remaining_large)
    assert max_accepted_poll_result_bytes(small_item) == min(MAX_POLL_DONE_RESULT_BYTES, remaining_small)
