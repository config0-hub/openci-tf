"""Production-shaped tests for acceptance-v12 catch/finalizer, child-output, and error presence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest  # type: ignore[import-not-found]

from src.core.errors import MalformedResultError
from src.domain.engine.artifact_limits import (
    MAX_OUTER_MAP_OUTCOME_BYTES,
    STEP_FUNCTIONS_STATE_LIMIT,
)
from src.domain.engine.artifact_paths import build_folder_artifact_keys, manifest_key
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.inner_state import serialized_state_bytes
from src.domain.engine.outer_map_state import (
    BOUNDED_TASK_CATCH_ERROR,
    apply_finalize_run_input_transition,
    apply_finalize_run_result_transition,
    apply_map_outcomes_transition,
    apply_render_pr_catch_transition,
    build_compact_resolve_result,
    validate_outer_transition_sequence,
)
from src.domain.engine.plan_artifacts import expected_plan_artifact_uris
from src.domain.engine.result import ExecutionResult, parse_result
from src.domain.engine.summary import bounded_summary, build_outer_map_outcome
from src.services.orchestration import finalize_run
from src.services.render import handler as render_handler
from src.services.run_folder import collect, write_failure_manifest
from tests.unit.manifest_fixtures import (
    committed_success_plan_manifest,
    complete_plan_object_mocks,
)
from tests.unit.test_acceptance_v10_outer_aggregate import (
    _engine_marker,
    _full_map_item,
    _handler_event,
    _map_merge_outcome,
    _maximum_folder_config,
    _valid_step,
)


def _max_repo_name() -> str:
    return "o" * 125 + "/" + "r" * 125


def _max_folder(index: int) -> str:
    return ("f" * 189) + f"{index:03d}"


def _production_child_success_output(item: dict) -> dict:
    folder = str(item["folder"])
    run_id = str(item["run_id"])
    attempt = int(item.get("attempt") or 0)
    exec_id = str(
        item.get("execution_id") or compose_execution_id(run_id, folder, attempt)
    )
    repo_name = str(item.get("repo_name") or _max_repo_name())
    tmp_bucket = "openci-tf-tmp-123456789012"
    done_bucket = "openci-tf-done-123456789012"
    folder_keys = build_folder_artifact_keys(
        repo_name=repo_name, run_id=run_id, folder_path=folder
    )
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
        ExecutionResult(exec_id, True, [], None), pointers, attempt=attempt
    )
    summary["manifest_s3_uri"] = (
        f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}"
    )
    summary["manifest_sha256"] = "a" * 64
    return summary


def test_rendered_catch_and_finalizer_transitions_discard_unbounded_envelopes():
    source = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text(
        encoding="utf-8"
    )
    import re

    for state_name in re.findall(r"^      (\w+) = \{$", source, re.MULTILINE):
        block = source.split(f"{state_name} = {{", 1)[1].split("\n      }", 1)[0]
        if "Catch" not in block:
            continue
        assert "ResultPath = null" in block, state_name
        assert 'ResultPath = "$.error"' not in block, state_name
    iterator = source.split("Iterator = {", 1)[1].split("\n        }", 1)[0]
    assert "normalize_folder_outcome = true" in iterator
    assert '"state.$"' in iterator
    assert "$.error.Cause" not in iterator
    assert (
        "ResultPath = null"
        in source.split("FinalizeRun = {", 1)[1].split("\n      }", 1)[0]
    )


def test_render_pr_and_finalize_catch_transitions_keep_post_map_state_bounded():
    folders = [_max_folder(index) for index in range(50)]
    repo = _max_repo_name()
    items = []
    for folder in folders:
        item = _full_map_item(folder)
        item["repo_name"] = repo
        item["folder"] = folder
        items.append(item)
    resolved = build_compact_resolve_result(
        _handler_event(), run_id="r" * 32, full_items=items, skipped=[]
    )
    outcomes = [
        build_outer_map_outcome(
            folder=str(item["folder"]),
            account_id=str(item["account_id"]),
            execution_id=str(item["execution_id"]),
            output=_production_child_success_output(item),
        )
        for item in items
    ]
    post_map = apply_map_outcomes_transition(resolved, outcomes)
    post_render_catch = apply_render_pr_catch_transition(post_map)
    finalize_input = apply_finalize_run_input_transition(post_render_catch)
    apply_finalize_run_result_transition(finalize_input, {"finalized": True})
    assert serialized_state_bytes(post_render_catch) <= STEP_FUNCTIONS_STATE_LIMIT


def test_four_kib_causes_never_retained_in_map_failure_or_task_catch_shapes():
    folders = [_max_folder(index) for index in range(50)]
    items = [
        _full_map_item(folder, config=_maximum_folder_config()) for folder in folders
    ]
    resolved = build_compact_resolve_result(
        _handler_event(), run_id="r" * 32, full_items=items, skipped=[]
    )
    task_catch = [
        build_outer_map_outcome(
            folder=str(item["folder"]),
            account_id=str(item["account_id"]),
            execution_id=str(item["e"]),
            status="infrastructure_error",
            error=BOUNDED_TASK_CATCH_ERROR,
            attempt=0,
        )
        for item in resolved["map_items"]
    ]
    post_task_catch = apply_map_outcomes_transition(resolved, task_catch)
    assert serialized_state_bytes(post_task_catch) < STEP_FUNCTIONS_STATE_LIMIT
    assert all(len(str(item.get("error") or "")) <= 256 for item in task_catch)
    apply_render_pr_catch_transition({**resolved, "outcomes": task_catch})


def test_maximum_collect_output_reaches_renderer_with_plan_pointer(monkeypatch):
    folder = _max_folder(0)
    repo = _max_repo_name()
    run = "r" * 32
    exec_id = compose_execution_id(run, folder, 0)
    sha = "a" * 40
    tmp_bucket = "tmp"
    done_bucket = "done"
    package_bucket = "pkg"
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
    }
    monkeypatch.setenv("TMP_BUCKET_NAME", tmp_bucket)
    monkeypatch.setenv("DONE_BUCKET_NAME", done_bucket)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", package_bucket)
    monkeypatch.delenv("RUN_REGISTRY_TABLE_NAME", raising=False)
    monkeypatch.setattr(collect, "build_manifest", lambda **_kwargs: manifest)
    monkeypatch.setattr(
        collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata
    )
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    summary = collect.handler(
        {
            "exec_id": exec_id,
            "attempt": 0,
            "deadline_at": "2999-01-01T00:00:00Z",
            "succeeded": True,
            "credential_expired": False,
            "steps": [_valid_step()],
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
        },
        object(),
    )
    outcome = build_outer_map_outcome(
        folder=folder,
        account_id="123456789012",
        execution_id=exec_id,
        output=summary,
    )
    assert serialized_state_bytes(outcome) <= MAX_OUTER_MAP_OUTCOME_BYTES
    monkeypatch.setenv("TMP_BUCKET_NAME", tmp_bucket)
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(
        render_handler.boto3,
        "resource",
        lambda *_args, **_kwargs: SimpleNamespace(Table=lambda *_a, **_k: object()),
    )
    monkeypatch.setattr(
        render_handler.run_lock, "release", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        render_handler, "_update_run_registry", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        render_handler,
        "_plan_artifact_metadata",
        lambda *_args, **_kwargs: plan_metadata,
    )
    render_handler.handler(
        {
            "run_id": run,
            "action": "plan",
            "notification_target": {"type": "registry"},
            "webhook_info": {"repo_name": repo, "commit_hash": sha},
            "settings": {"ssm_openci_tf_github_token": "/token"},
            "outcomes": [outcome],
        },
        object(),
    )


def test_committed_success_failure_writer_preserves_plan_metadata_pointer(monkeypatch):
    folder = _max_folder(0)
    repo = _max_repo_name()
    run = "r" * 32
    exec_id = compose_execution_id(run, folder, 0)
    sha = "a" * 40
    tmp_bucket = "openci-tf-tmp-123456789012"
    expected = expected_plan_artifact_uris(
        bucket=tmp_bucket,
        repo_name=repo,
        run_id=run,
        folder_path=folder,
    )
    committed = committed_success_plan_manifest(
        execution_id=exec_id,
        tmp_bucket=tmp_bucket,
        repo_name=repo,
        run_id=run,
        commit_hash=sha,
        account_id="123456789012",
        folder=folder,
        attempt=0,
    )
    from src.domain.engine.manifest import _canonical_manifest_digest

    committed["manifest_sha256"] = _canonical_manifest_digest(committed)
    monkeypatch.setenv("TMP_BUCKET_NAME", tmp_bucket)
    done_bucket = "done"
    monkeypatch.setenv("DONE_BUCKET_NAME", done_bucket)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.delenv("RUN_REGISTRY_TABLE_NAME", raising=False)
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    monkeypatch.setattr(
        write_failure_manifest, "get_bounded_json", lambda *_args, **_kwargs: committed
    )
    summary = write_failure_manifest.handler(
        {
            "run_id": run,
            "folder": folder,
            "action": "plan",
            "account_id": "123456789012",
            "attempt": 0,
            "exec_id": exec_id,
            "deadline_at": "2999-01-01T00:00:00Z",
            "failure_reason": "late failure",
            "repo_name": repo,
            "commit_hash": sha,
            "submitted_at": 1_700_000_000.0,
        },
        object(),
    )
    assert summary["pointers"]["plan_metadata"] == expected.metadata
    assert summary["pointers"]["done"] == f"s3://{done_bucket}/{exec_id}/done"
    assert summary["succeeded"] is True


@pytest.mark.parametrize(
    "marker",
    [
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [_valid_step()],
            "error": None,
        },
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [_valid_step()],
            "error": "",
        },
        {
            "trigger_id": "run",
            "status": "failed",
            "steps": [_valid_step(status="failed", exit_code=1, output="x")],
            "error": None,
        },
        {
            "trigger_id": "run",
            "status": "failed",
            "steps": [_valid_step(status="failed", exit_code=1, output="x")],
            "error": "",
        },
    ],
)
def test_top_level_error_presence_rejects_impossible_markers(marker):
    with pytest.raises(MalformedResultError):
        parse_result(marker, "run")


def test_failed_marker_requires_non_empty_error_when_key_present():
    result = parse_result(
        _engine_marker("run", status="failed", exit_code=1, output="boom"),
        "run",
    )
    assert result.error == "boom"


def test_failed_marker_without_error_key_derives_from_steps():
    marker = {
        "trigger_id": "run",
        "status": "failed",
        "steps": [_valid_step(status="failed", exit_code=1, output="Error: derived")],
    }
    result = parse_result(marker, "run")
    assert "derived" in str(result.error)


def test_fifty_exact_production_shapes_fit_all_rendered_transitions():
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
    for builder in (
        lambda item: _map_merge_outcome(
            str(item["folder"]), _production_child_success_output(item)
        ),
        lambda item: _map_merge_outcome(
            str(item["folder"]),
            bounded_summary(
                ExecutionResult(str(item["execution_id"]), False, [], "x" * 200),
                {"artifacts_prefix": f"s3://tmp/{item['execution_id']}/"},
                attempt=0,
            )
            | {
                "manifest_s3_uri": f"s3://tmp/{item['execution_id']}/manifest.json",
                "manifest_sha256": "a" * 64,
            },
        ),
        lambda item: build_outer_map_outcome(
            folder=str(item["folder"]),
            account_id=str(item["account_id"]),
            execution_id=str(item["execution_id"]),
            status="infrastructure_error",
            error="malformed child execution output",
        ),
        lambda item: build_outer_map_outcome(
            folder=str(item["folder"]),
            account_id=str(item["account_id"]),
            execution_id=str(item["execution_id"]),
            status="infrastructure_error",
            error=BOUNDED_TASK_CATCH_ERROR,
            attempt=0,
        ),
    ):
        outcomes = [builder(item) for item in items]
        validate_outer_transition_sequence(
            validate_output=resolved,
            placeholder_result={"placeholder_rendered": True},
            outcomes=outcomes,
        )


def test_render_pr_failure_still_reaches_finalize_run_for_lock_cleanup(monkeypatch):
    released: list[tuple[str, str]] = []

    def release(table, repo, folder, execution_id):
        released.append((folder, execution_id))

    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(finalize_run, "dynamo_table", lambda *_: object())
    monkeypatch.setattr(
        finalize_run.run_lock,
        "release_all",
        lambda _table, _run_id: release(_table, "org/repo", "infra/a", "e1") or 1,
    )
    monkeypatch.setattr(finalize_run, "get_folder_attempt", lambda *_args: None)
    monkeypatch.setattr(finalize_run, "put_folder_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        finalize_run, "finalize_run_if_running", lambda *_args, **_kwargs: None
    )
    event = {
        "run_id": "run",
        "webhook_info": {"repo_name": "org/repo"},
        "map_items": [
            {"folder": "infra/a", "account_id": "123456789012", "execution_id": "e1"}
        ],
        "outcomes": [],
    }
    apply_finalize_run_input_transition(event)
    result = finalize_run.handler(event, object())
    assert result == {"finalized": True}
    assert released == [("infra/a", "e1")]


def test_normalize_config_error_delegates_bounded_shaping_to_consumer():
    source = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text(
        encoding="utf-8"
    )
    block = source.split("NormalizeConfigError = {", 1)[1].split("\n      }", 1)[0]
    assert 'Type     = "Task"' in block
    assert "normalize_config_error = true" in block
    assert '"state.$"              = "$"' in block
    assert "$.error.Cause" not in block
