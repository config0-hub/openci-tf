"""Production-shaped tests for acceptance-v5 blockers C1-C3 and H1."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.domain.engine.lifecycle import (
    conservative_api_expiry_iso,
    done_retention_days,
    package_retention_days,
    tmp_retention_days,
)
from src.domain.engine.manifest import (
    _canonical_manifest_digest,
    validate_manifest_schema,
)
from src.domain.engine.plan_artifacts import plan_retention_days
from src.services.run_folder import collect
from tests.unit.manifest_fixtures import (
    committed_success_plan_manifest,
    complete_plan_object_mocks,
)


def _outer_states() -> dict[str, str]:
    definition = (Path(__file__).parents[2] / "infra/deploy/modules/openci_tf/step_function.tf").read_text()
    states: dict[str, str] = {}
    for match in re.finditer(r"^      (?P<name>\w+) = \{", definition, re.MULTILINE):
        block = definition.split(f"{match.group('name')} = {{", 1)[1].split("\n      }", 1)[0]
        states[match.group("name")] = block
    return states


def _digest_manifest(manifest: dict) -> dict:
    updated = copy.deepcopy(manifest)
    updated["manifest_sha256"] = _canonical_manifest_digest(updated)
    return updated


def test_prepare_lambda_receives_plan_retention_days():
    lambdas = (Path(__file__).parents[2] / "infra/deploy/modules/run_folder/lambdas.tf").read_text()
    assert 'contains(["collect", "write-failure-manifest", "prepare-and-submit"], each.key)' in lambdas
    assert "PLAN_RETENTION_DAYS" in lambdas


def test_non_default_lifecycle_values_are_consistent(monkeypatch):
    monkeypatch.setenv("TMP_LIFECYCLE_DAYS", "4")
    monkeypatch.setenv("PACKAGE_LIFECYCLE_DAYS", "31")
    monkeypatch.setenv("DONE_LIFECYCLE_DAYS", "366")
    monkeypatch.setenv("PLAN_RETENTION_DAYS", "7")
    assert tmp_retention_days() == 4
    assert package_retention_days() == 31
    assert done_retention_days() == 366
    assert plan_retention_days() == 7
    foundation = (Path(__file__).parents[2] / "infra/foundation/s3.tf").read_text()
    assert "expiration { days = var.plan_retention_days }" in foundation
    modified = datetime(2026, 8, 10, 15, 30, tzinfo=timezone.utc)
    assert conservative_api_expiry_iso(modified, 7) == "2026-08-17T00:00:00Z"


def test_lifecycle_env_parsing_fails_loud(monkeypatch):
    monkeypatch.setenv("PLAN_RETENTION_DAYS", "not-a-number")
    with pytest.raises(ValueError, match="PLAN_RETENTION_DAYS"):
        plan_retention_days()


def test_manifest_rejects_unrelated_plan_topology():
    manifest = committed_success_plan_manifest(
        execution_id="run.abc.0",
        commit_hash="c" * 40,
    )
    mutated = copy.deepcopy(manifest)
    mutated["entries"][7]["s3_uri"] = "s3://tmp/openci-tf/org/repo/run/unrelated/repo/sha/account/folder/execution/attempt/plan.tfplan"
    mutated = _digest_manifest(mutated)
    with pytest.raises(ValueError, match="exact binding URI"):
        validate_manifest_schema(mutated, execution_id="run.abc.0")


@pytest.mark.parametrize(
    ("entry_name", "content_type"),
    [
        ("plan.tfplan", "text/plain"),
        ("plan.tfplan.sha256", "application/json"),
        ("package", "text/html"),
    ],
)
def test_manifest_rejects_wrong_entry_content_types(entry_name: str, content_type: str):
    manifest = committed_success_plan_manifest(
        execution_id="run.abc.0",
        commit_hash="c" * 40,
    )
    mutated = copy.deepcopy(manifest)
    for entry in mutated["entries"]:
        if entry["name"] == entry_name:
            entry["content_type"] = content_type
    mutated = _digest_manifest(mutated)
    with pytest.raises(ValueError, match="content type"):
        validate_manifest_schema(mutated, execution_id="run.abc.0")


def test_manifest_rejects_cross_action_failure_inventory():
    manifest = committed_success_plan_manifest(
        execution_id="run.abc.0",
        commit_hash="c" * 40,
        action="drift",
    )
    mutated = copy.deepcopy(manifest)
    mutated["failure_reason"] = "drift failed"
    mutated["entries"] = [
        entry for entry in mutated["entries"] if entry["name"] in {"init.out", "validate.out", "tf/plan.out", "tfsec.json"}
    ]
    mutated = _digest_manifest(mutated)
    with pytest.raises(ValueError, match="unexpected entries for failed drift"):
        validate_manifest_schema(mutated, execution_id="run.abc.0")


def test_collect_rejects_wrong_existing_execution_id(monkeypatch):
    exec_id = "run.0123456789ab.0"
    other_exec_id = "other.0123456789ab.0"
    committed = committed_success_plan_manifest(
        execution_id=other_exec_id,
        commit_hash="c" * 40,
    )
    last_modified = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id,
        repo_name="org/repo",
        run_id="run",
        commit_hash="c" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        last_modified=last_modified,
    )
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata)
    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")))
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: committed)
    with pytest.raises(ValueError, match="execution_id mismatch"):
        collect.handler(
            {
                "exec_id": exec_id,
                "attempt": 0,
                "succeeded": False,
                "credential_expired": False,
                "steps": [],
                "error": "late failure",
                "pointers": {"done": f"s3://done/{exec_id}/done"},
                "action": "plan",
                "repo_name": "org/repo",
                "commit_hash": "c" * 40,
                "account_id": "123456789012",
                "folder": "infra/a",
                "run_id": "run",
                "submitted_at": 1_700_000_000.0,
                "plan_metadata_uri": plan_metadata["metadata_s3_uri"],
            },
            object(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "other-run"),
        ("repo_name", "other/repo"),
        ("commit_hash", "d" * 40),
        ("account_id", "210987654321"),
        ("folder", "infra/b"),
        ("action", "drift"),
        ("attempt", 1),
    ],
)
def test_collect_rejects_mismatched_existing_binding(field: str, value: object, monkeypatch):
    from src.domain.engine.execution_id import compose_execution_id

    exec_id = compose_execution_id("run", "infra/a", 0)
    committed = committed_success_plan_manifest(
        execution_id=exec_id,
        commit_hash="c" * 40,
    )
    last_modified = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id,
        repo_name="org/repo",
        run_id="run",
        commit_hash="c" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        last_modified=last_modified,
    )
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")))
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: committed)
    event = {
        "exec_id": exec_id,
        "attempt": 0,
        "succeeded": False,
        "credential_expired": False,
        "steps": [],
        "error": "late failure",
        "pointers": {"done": f"s3://done/{exec_id}/done"},
        "action": "plan",
        "repo_name": "org/repo",
        "commit_hash": "c" * 40,
        "account_id": "123456789012",
        "folder": "infra/a",
        "run_id": "run",
        "submitted_at": 1_700_000_000.0,
        "plan_metadata_uri": plan_metadata["metadata_s3_uri"],
    }
    event[field] = value
    with pytest.raises(ValueError):
        collect.handler(event, object())


def test_map_failure_asl_routes_through_render_before_finalize():
    states = _outer_states()
    run_folders = states["RunFolders"]
    assert 'Next = "FailRunFolders"' in run_folders
    fail_run_folders = states["FailRunFolders"]
    assert 'Next       = "RenderPipelineFailure"' in fail_run_folders
    render_pipeline_failure = states["RenderPipelineFailure"]
    assert 'local.lambda_arns["render-pr"]' in render_pipeline_failure
    assert 'Next       = "FinalizeRun"' in render_pipeline_failure
    finalize_run = states["FinalizeRun"]
    assert 'Next       = "PipelineFailed"' in finalize_run
    assert "RouteAfterFinalize" not in states
    assert "ConfigResolutionFailed" not in states
    assert states["PipelineFailed"].strip().startswith('Type  = "Fail"')


def test_map_failure_routes_through_render_pipeline_failure_not_unreachable_state():
    states = _outer_states()
    assert "NormalizeMapFailure" not in states
