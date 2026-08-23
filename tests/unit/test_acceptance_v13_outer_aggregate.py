"""Production-shaped tests for acceptance-v13 projection, identity, catch, and schema fixes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.domain.engine.artifact_limits import (
    MAX_DEPLOYMENT_NAME_PREFIX_CHARS,
    MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES,
    MAX_OUTER_MAP_OUTCOME_BYTES,
    STEP_FUNCTIONS_STATE_LIMIT,
)
from src.domain.engine.deployment_buckets import maximum_foundation_bucket_names
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.inner_state import serialized_state_bytes
from src.domain.engine.outer_map_state import (
    BOUNDED_TASK_CATCH_ERROR,
    build_compact_resolve_result,
    validate_outer_resolve_result,
)
from src.domain.engine.artifact_paths import build_folder_artifact_keys, manifest_key
from src.domain.engine.plan_artifacts import expected_plan_artifact_uris
from src.domain.engine.result import ExecutionResult
from src.domain.engine.summary import bounded_summary, build_outer_map_outcome
from src.domain.run.outcome import normalize_map_outcome
from src.services.render import handler as render_handler
from src.services.run_folder import collect, write_failure_manifest
from tests.unit.manifest_fixtures import (
    committed_success_plan_manifest,
    complete_plan_object_mocks,
)
from tests.unit.test_acceptance_v10_outer_aggregate import (
    _full_map_item,
    _handler_event,
    _maximum_folder_config,
    _valid_step,
)

_SOURCE = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text(encoding="utf-8")


def _max_folder(index: int) -> str:
    return ("f" * 189) + f"{index:03d}"


def _max_repo_name() -> str:
    return "o" * 125 + "/" + "r" * 125


def _state_block(name: str) -> str:
    return _SOURCE.split(f"{name} = {{", 1)[1].split("\n      }", 1)[0]


def _iterator_block() -> str:
    return _SOURCE.split("Iterator = {", 1)[1].split("\n        }", 1)[0]


def _production_child_success_output(item: dict, *, tmp_bucket: str, done_bucket: str) -> dict:
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
        ExecutionResult(exec_id, True, [], None), pointers, attempt=attempt
    )
    summary["manifest_s3_uri"] = f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}"
    summary["manifest_sha256"] = "a" * 64
    return summary


def _fifty_maximum_items() -> list[dict]:
    folders = [_max_folder(index) for index in range(50)]
    repo = _max_repo_name()
    config = _maximum_folder_config()
    items: list[dict] = []
    for folder in folders:
        item = _full_map_item(folder, config=config)
        item["repo_name"] = repo
        item["folder"] = folder
        items.append(item)
    return items


def test_every_outer_catch_discards_unbounded_aws_envelopes():
    for state_name in re.findall(r"^      (\w+) = \{$", _SOURCE, re.MULTILINE):
        block = _state_block(state_name)
        if "Catch" not in block:
            continue
        assert "ResultPath = null" in block, state_name
        assert 'ResultPath = "$.error"' not in block, state_name
    iterator = _iterator_block()
    assert "Catch" in iterator
    assert "ResultPath = null" in iterator
    assert "$.error" not in iterator


def test_rendered_iterator_delegates_bounded_normalization_to_consumer_lambda():
    iterator = _iterator_block()
    assert "NormalizeMalformedChildOutcome" not in iterator
    assert "MergeFolderOutcome" not in iterator
    block = iterator.split("NormalizeFolderOutcome = {", 1)[1]
    assert 'Type     = "Task"' in block
    assert 'local.lambda_arns["render-pr"]' in block
    assert "normalize_folder_outcome = true" in block
    assert '"state.$"                = "$"' in block


def test_fifty_exact_production_successes_fit_aggregate_and_post_map_budget():
    items = _fifty_maximum_items()
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
    assert serialized_state_bytes(post_map) <= STEP_FUNCTIONS_STATE_LIMIT
    validate_outer_resolve_result(resolved)


def test_rendered_task_catch_and_malformed_outcomes_release_real_locks(monkeypatch):
    released: list[tuple[str, str]] = []
    folder = _max_folder(0)
    run_id = "r" * 32
    exec_id = compose_execution_id(run_id, folder, 0)
    rendered_shapes = [
        {
            "folder": folder,
            "account_id": "123456789012",
            "execution_id": exec_id,
            "attempt": 0,
            "status": "infrastructure_error",
            "succeeded": False,
            "error": "malformed child execution output",
        },
        {
            "folder": folder,
            "account_id": "123456789012",
            "execution_id": exec_id,
            "attempt": 0,
            "status": "infrastructure_error",
            "succeeded": False,
            "error": BOUNDED_TASK_CATCH_ERROR,
        },
    ]
    for raw in rendered_shapes:
        normalized = normalize_map_outcome(raw)
        assert normalized["execution_id"] == exec_id
        assert normalized["succeeded"] is False
        monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
        monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setattr(
            render_handler.boto3,
            "resource",
            lambda *_args, **_kwargs: SimpleNamespace(Table=lambda *_a, **_k: object()),
        )
        monkeypatch.setattr(
            render_handler.run_lock,
            "release",
            lambda _table, _repo, folder_name, execution_id: released.append(
                (folder_name, execution_id)
            ),
        )
        monkeypatch.setattr(
            render_handler, "_update_run_registry", lambda *_args, **_kwargs: None
        )
        render_handler.handler(
            {
                "run_id": run_id,
                "action": "plan",
                "notification_target": {"type": "registry"},
                "webhook_info": {"repo_name": _max_repo_name(), "commit_hash": "a" * 40},
                "settings": {"ssm_openci_tf_github_token": "/token"},
                "outcomes": [normalized],
            },
            object(),
        )
    assert released == [(folder, exec_id), (folder, exec_id)]


def test_maximum_canonical_installation_collect_and_reconciliation_share_schema(
    monkeypatch,
):
    folder = _max_folder(0)
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
    collect_summary = collect.handler(
        {
            "exec_id": exec_id,
            "attempt": 0,
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
    collect_outcome = build_outer_map_outcome(
        folder=folder,
        account_id="123456789012",
        execution_id=exec_id,
        output=collect_summary,
    )
    assert serialized_state_bytes(collect_outcome) <= MAX_OUTER_MAP_OUTCOME_BYTES
    assert collect_summary["pointers"]["done"] == f"s3://{done_bucket}/{exec_id}/done"
    assert collect_summary["pointers"]["plan_metadata"] == uris.metadata

    committed = committed_success_plan_manifest(
        execution_id=exec_id,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        package_bucket=package_bucket,
        repo_name=repo,
        run_id=run,
        commit_hash=sha,
        account_id="123456789012",
        folder=folder,
        attempt=0,
    )
    from src.domain.engine.manifest import _canonical_manifest_digest

    committed["manifest_sha256"] = _canonical_manifest_digest(committed)
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    monkeypatch.setattr(
        write_failure_manifest, "get_bounded_json", lambda *_args, **_kwargs: committed
    )
    reconciled = write_failure_manifest.handler(
        {
            "run_id": run,
            "folder": folder,
            "action": "plan",
            "account_id": "123456789012",
            "attempt": 0,
            "exec_id": exec_id,
            "failure_reason": "late failure",
            "repo_name": repo,
            "commit_hash": sha,
            "submitted_at": 1_700_000_000.0,
        },
        object(),
    )
    for key in ("done", "plan_metadata", "artifacts_prefix"):
        assert reconciled["pointers"][key] == collect_summary["pointers"][key]
    assert reconciled["succeeded"] is True
    assert set(reconciled["pointers"]) == set(collect_summary["pointers"])


def test_deployment_bucket_bound_matches_installer_prefix_limit():
    buckets = maximum_foundation_bucket_names()
    prefix = "p" * MAX_DEPLOYMENT_NAME_PREFIX_CHARS
    assert buckets["tmp"] == f"{prefix}-tmp-123456789012"
    assert len(buckets["tmp"]) == 59
    assert len(buckets["done"]) == 60
    assert len(buckets["package"]) == 63
