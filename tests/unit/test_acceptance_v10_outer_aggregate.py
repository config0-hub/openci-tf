"""Production-shaped tests for acceptance-v10 outer aggregate and strict engine steps."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.errors import (
    ConfigResolutionError,
    ConfigValidationError,
    MalformedResultError,
)
from src.domain.config.folder_config import compact_folder_config_for_outer_state, parse_folder_config
from src.domain.engine.artifact_limits import (
    MAX_OUTER_MAP_OUTCOME_BYTES,
    MAX_OUTER_VALIDATE_BYTES,
    MAX_POLL_DONE_RESULT_BYTES,
    STEP_FUNCTIONS_STATE_LIMIT,
)
from src.domain.engine.artifact_paths import (
    build_folder_artifact_keys,
    expected_plan_artifact_uris,
    manifest_key,
)
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.inner_state import serialized_state_bytes
from src.domain.engine.outer_map_state import (
    apply_map_outcomes_transition,
    apply_placeholder_transition,
    build_compact_resolve_result,
    merge_map_item,
    outer_state_budget_summary,
    validate_outer_transition_sequence,
)
from src.domain.engine.result import (
    bound_step_metadata,
    parse_result,
)
from src.domain.engine.summary import MAX_SUMMARY_BYTES, bounded_summary
from src.services.render import handler as render_handler
from src.services.resolve import validate_and_resolve
from src.services.run_folder import poll_done, write_failure_manifest


def _valid_step(**overrides: object) -> dict:
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
    exec_id: str, *, status: str = "succeeded", **step_overrides: object
) -> dict:
    step = _valid_step(
        status="failed" if status == "failed" else "succeeded", **step_overrides
    )
    if status == "failed" and step["exit_code"] == 0:
        step["exit_code"] = 1
    marker: dict = {"trigger_id": exec_id, "status": status, "steps": [step]}
    return marker


def _maximum_outer_folder_yaml() -> str:
    prefix = "/openci-tf/env/"
    paths = [
        prefix + ("x" * (256 - len(prefix) - 2)) + f"{index:02x}" for index in range(4)
    ]
    flags = ["f" * 512]
    return (
        "account_alias: '"
        + ("a" * 128)
        + "'\n"
        + "extra_flags:\n"
        + "".join(f"  - '{flag}'\n" for flag in flags)
        + "ssm_env_paths:\n"
        + "".join(f"  - '{path}'\n" for path in paths)
    )


def _maximum_folder_config() -> dict:
    return compact_folder_config_for_outer_state(asdict(parse_folder_config(_maximum_outer_folder_yaml())))


def _upstream_urls() -> dict[str, str]:
    return {
        "tofu:1.8.0": "https://example.invalid/" + ("t" * 2000),
        "tfsec:1.28.10": "https://example.invalid/" + ("s" * 2000),
        "infracost:0.10.39": "https://example.invalid/" + ("i" * 2000),
    }


def _full_map_item(folder: str, *, config: dict | None = None) -> dict:
    config = config or _maximum_folder_config()
    upstream = _upstream_urls()
    run_id = "r" * 32
    return {
        "run_id": run_id,
        "folder": folder,
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
        "folder_config": config,
        "upstream_urls": upstream,
        "execution_id": compose_execution_id(run_id, folder, 0),
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
    }


def _handler_event() -> dict:
    return {
        "run_id": "r" * 32,
        "webhook_info": {
            "event_type": "api",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "trigger_id": "trigger",
            "idempotency_key": "delivery1",
        },
        "settings": {
            "git_url": "https://github.com/org/repo.git",
            "ssm_openci_tf_github_token": "/openci-tf/github/token",
            "ssm_infracost_api_key": "/openci-tf/infracost/key",
            "upstream_urls": _upstream_urls(),
        },
        "action": "plan",
        "folders": [],
        "all_flag": True,
        "affected_flag": False,
        "notification_target": {"type": "registry"},
    }


def _wire_handler(monkeypatch, *, folders: list[str], config: dict) -> list[tuple]:
    acquired: list[tuple] = []
    released: list[tuple] = []

    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(
        validate_and_resolve.boto3,
        "resource",
        lambda *_: SimpleNamespace(Table=lambda _: object()),
    )
    monkeypatch.setattr(validate_and_resolve, "get_github_token", lambda *_: "token")
    monkeypatch.setattr(
        validate_and_resolve, "validate_clone_source", lambda value, *_: value
    )
    monkeypatch.setattr(
        validate_and_resolve,
        "shallow_clone",
        lambda *_args, **_kwargs: "/tmp/fake-clone",
    )
    monkeypatch.setattr(validate_and_resolve, "cleanup_clone", lambda *_: None)
    monkeypatch.setattr(
        validate_and_resolve, "validate_reserved_package_names", lambda *_: None
    )
    monkeypatch.setattr(validate_and_resolve, "_selected_folders", lambda *_: folders)
    monkeypatch.setattr(
        validate_and_resolve,
        "resolve_outer_state",
        lambda *_: {
            "folder_configs": {folder: config for folder in folders},
            "upstream_urls": _upstream_urls(),
        },
    )
    monkeypatch.setattr(
        validate_and_resolve,
        "load_account_alias",
        lambda *_: SimpleNamespace(
            account_id="123456789012",
            role_name="openci-tf-executor-readonly",
            poweruser_role_name=None,
            external_id="openci-tf-0123456789abcdef",
            max_ttl=3600,
        ),
    )

    def acquire(table, repo, folder, execution_id, now, budget, *ownership):
        acquired.append((folder, execution_id))

    def release(table, repo, folder, execution_id):
        released.append((folder, execution_id))

    def release_all(table, run_id):
        for folder, execution_id in acquired:
            validate_and_resolve.run_lock.release(
                table, "org/repo", folder, execution_id
            )
        return len(acquired)

    monkeypatch.setattr(validate_and_resolve.run_lock, "acquire", acquire)
    monkeypatch.setattr(validate_and_resolve.run_lock, "release", release)
    monkeypatch.setattr(validate_and_resolve.run_lock, "release_all", release_all)
    return acquired


def _map_merge_outcome(folder: str, child_output: dict) -> dict:
    execution_id = (
        child_output.get("exec_id")
        or child_output.get("execution_id")
        or f"run.{folder[-2:]}.0"
    )
    return {
        "folder": folder,
        "account_id": "123456789012",
        "execution_id": execution_id,
        "output": child_output,
    }


def _maximum_child_success_output(folder: str) -> dict:
    item = _full_map_item(folder)

    exec_id = str(item["execution_id"])
    run_id = str(item["run_id"])
    repo_name = str(item["repo_name"])
    tmp_bucket = "openci-tf-tmp-123456789012"
    done_bucket = "openci-tf-done-123456789012"
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
        parse_result(_engine_marker(exec_id), exec_id),
        pointers,
        attempt=0,
    )
    summary["manifest_s3_uri"] = f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}"
    summary["manifest_sha256"] = "a" * 64
    return summary


def _maximum_child_failure_output(folder: str) -> dict:
    item = _full_map_item(folder)
    exec_id = str(item["execution_id"])
    run_id = str(item["run_id"])
    repo_name = str(item["repo_name"])
    tmp_bucket = "openci-tf-tmp-123456789012"
    folder_keys = build_folder_artifact_keys(repo_name=repo_name, run_id=run_id, folder_path=folder)

    pointers = {
        "manifest": f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}",
        "artifacts_prefix": f"s3://{tmp_bucket}/{folder_keys.prefix}",
    }
    summary = bounded_summary(
        parse_result(
            _engine_marker(
                exec_id, status="failed", exit_code=1, output="Error: " + ("x" * 400)
            ),
            exec_id,
        ),
        pointers,
        attempt=0,
    )
    summary["manifest_s3_uri"] = pointers["manifest"]
    summary["manifest_sha256"] = "a" * 64
    return summary


def _maximum_map_failure_outcome(folder: str) -> dict:
    item = _full_map_item(folder)
    return {
        "folder": folder,
        "account_id": "123456789012",
        "execution_id": str(item["execution_id"]),
        "attempt": 0,
        "status": "infrastructure_error",
        "succeeded": False,
        "error": "map execution failed",
    }


def test_rendered_asl_placeholder_discards_lambda_result():
    source = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text(
        encoding="utf-8"
    )
    block = source.split("RenderPlaceholder = {", 1)[1].split("\n      }", 1)[0]
    assert "ResultPath = null" in block


def test_placeholder_handler_returns_tiny_ack_without_duplicating_input(monkeypatch):
    from types import SimpleNamespace

    event = {
        "placeholder": True,
        "action": "plan",
        "webhook_info": {
            "repo_name": "org/repo",
            "pr_number": 7,
            "commit_hash": "a" * 40,
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
        "map_items": [{"folder": "infra/a", "account_id": "123456789012"}],
        "skipped": [],
        "map_shared": {"repo_name": "org/repo"},
    }
    monkeypatch.setattr(render_handler, "get_github_token", lambda *_: "token")
    monkeypatch.setattr(
        render_handler,
        "GitHubClient",
        lambda *_: SimpleNamespace(
            create_comment=lambda *_args, **_kwargs: 1,
            delete_and_repost=lambda *_args, **_kwargs: 1,
            find_comments_by_tag=lambda *_args, **_kwargs: [],
            delete_comment=lambda *_args, **_kwargs: None,
        ),
    )
    monkeypatch.setattr(
        render_handler, "_delete_and_repost", lambda *_args, **_kwargs: 1
    )
    result = render_handler.handler(event, None)
    assert result == {"placeholder_rendered": True}
    assert "map_items" not in result
    post = apply_placeholder_transition(event, result)
    assert serialized_state_bytes(post) == serialized_state_bytes(event)


def test_fifty_maximum_success_and_failure_outcomes_fit_outer_transition_budget():
    folders = [f"infra/folder-{index:02d}" for index in range(50)]
    items = [_full_map_item(folder) for folder in folders]
    resolved = build_compact_resolve_result(
        _handler_event(), run_id="r" * 32, full_items=items, skipped=[]
    )
    success_outcomes = [
        _map_merge_outcome(folder, _maximum_child_success_output(folder))
        for folder in folders
    ]
    validate_outer_transition_sequence(
        validate_output=resolved,
        placeholder_result={"placeholder_rendered": True},
        outcomes=success_outcomes,
    )
    failure_outcomes = [
        _map_merge_outcome(folder, _maximum_child_failure_output(folder))
        for folder in folders
    ]
    validate_outer_transition_sequence(
        validate_output=resolved,
        placeholder_result={"placeholder_rendered": True},
        outcomes=failure_outcomes,
    )
    map_failure_outcomes = [_maximum_map_failure_outcome(folder) for folder in folders]
    apply_map_outcomes_transition(resolved, map_failure_outcomes)


def test_post_lock_budget_failure_releases_all_locks(monkeypatch):
    folders = [f"infra/folder-{index:02d}" for index in range(50)]
    config = _maximum_folder_config()
    acquired = _wire_handler(monkeypatch, folders=folders, config=config)
    released: list[tuple[str, str]] = []

    def release(table, repo, folder, execution_id):
        released.append((folder, execution_id))

    monkeypatch.setattr(validate_and_resolve.run_lock, "release", release)

    original_build = validate_and_resolve.build_compact_resolve_result
    build_calls = {"count": 0}

    def exploding_build(*args, **kwargs):
        build_calls["count"] += 1
        result = original_build(*args, **kwargs)
        if build_calls["count"] > 1 and kwargs.get("full_items"):
            raise ConfigResolutionError(
                "outer state exceeds Step Functions budget at validate-and-resolve"
            )
        return result

    monkeypatch.setattr(
        validate_and_resolve, "build_compact_resolve_result", exploding_build
    )
    with pytest.raises(ConfigResolutionError, match="outer state exceeds"):
        validate_and_resolve.handler(_handler_event(), object())
    assert len(acquired) == 50
    assert len(released) == 50


def test_succeeded_with_error_and_unknown_top_level_fields_rejected():
    with pytest.raises(MalformedResultError):
        parse_result(
            {
                "trigger_id": "run",
                "status": "succeeded",
                "steps": [_valid_step()],
                "error": "impossible success error",
            },
            "run",
        )
    with pytest.raises(MalformedResultError):
        parse_result(
            {
                "trigger_id": "run",
                "status": "succeeded",
                "steps": [_valid_step()],
                "error": None,
            },
            "run",
        )
    with pytest.raises(MalformedResultError):
        parse_result(
            {
                "trigger_id": "run",
                "status": "succeeded",
                "steps": [_valid_step()],
                "unexpected": True,
            },
            "run",
        )


def test_negative_signal_exit_code_accepted_as_coherent_failure():
    result = parse_result(
        _engine_marker("run", status="failed", exit_code=-9, output="killed"),
        "run",
    )
    assert not result.succeeded
    assert result.steps[0]["exit_code"] == -9


def test_invalid_signed_exit_ranges_and_bool_exit_rejected():
    with pytest.raises(MalformedResultError):
        parse_result(
            _engine_marker("run", status="failed", exit_code=-129, output=""), "run"
        )
    with pytest.raises(MalformedResultError):
        parse_result(
            _engine_marker("run", status="failed", exit_code=256, output=""), "run"
        )
    with pytest.raises(MalformedResultError):
        parse_result(
            {
                "trigger_id": "run",
                "status": "failed",
                "steps": [_valid_step(status="failed", exit_code=True, output="")],
            },
            "run",
        )


def test_each_map_outcome_stays_within_per_child_budget():
    folder = "infra/folder-00"
    outcome = _map_merge_outcome(folder, _maximum_child_failure_output(folder))
    assert serialized_state_bytes(outcome) <= MAX_OUTER_MAP_OUTCOME_BYTES


def test_bounded_summary_budget_tracks_complete_child_limit():
    assert MAX_SUMMARY_BYTES == MAX_OUTER_MAP_OUTCOME_BYTES


def test_outer_state_budget_has_substantial_headroom():
    summary = outer_state_budget_summary()
    assert summary["max_outer_post_map_state_bytes"] < STEP_FUNCTIONS_STATE_LIMIT
    assert summary["headroom_bytes"] >= 1_000


def test_fifty_maximum_accepted_folder_configurations_fit_outer_budget():
    folders = [f"infra/folder-{index:02d}" for index in range(50)]
    items = [_full_map_item(folder) for folder in folders]
    result = build_compact_resolve_result(
        _handler_event(), run_id="r" * 32, full_items=items, skipped=[]
    )
    assert len(result["map_items"]) == 50
    assert "map_shared" in result
    assert serialized_state_bytes(result) < MAX_OUTER_VALIDATE_BYTES
    assert serialized_state_bytes(result) < 200_000


def test_actual_handler_accepts_fifty_maximum_configurations(monkeypatch):
    folders = [f"infra/folder-{index:02d}" for index in range(50)]
    config = _maximum_folder_config()
    acquired = _wire_handler(monkeypatch, folders=folders, config=config)
    result = validate_and_resolve.handler(_handler_event(), object())
    assert len(result["map_items"]) == 50
    assert len(acquired) == 50
    assert serialized_state_bytes(result) < MAX_OUTER_VALIDATE_BYTES
    for field in ("upstream_urls", "git_url", "repo_name", "commit_hash"):
        assert field not in result["map_items"][0]
        assert field in result["map_shared"]


def test_legacy_six_folder_forty_five_kib_attack_rejected_before_locks(monkeypatch):
    prefix = "/openci-tf/env/"
    paths = [
        prefix + ("x" * (2048 - len(prefix) - 2)) + f"{index:02x}"
        for index in range(16)
    ]
    flags = ["f" * 4096 for _ in range(3)]
    yaml_text = (
        "account_alias: '"
        + ("a" * 128)
        + "'\n"
        + "extra_flags:\n"
        + "".join(f"  - '{flag}'\n" for flag in flags)
        + "ssm_env_paths:\n"
        + "".join(f"  - '{path}'\n" for path in paths)
    )
    with pytest.raises(ConfigValidationError):
        parse_folder_config(yaml_text)
    oversized = {
        "account_alias": "a" * 128,
        "tf_runtime": "tofu:1.8.0",
        "execution_target": "lambda",
        "timeout": 3600,
        "extra_flags": flags,
        "ssm_env_paths": paths,
    }
    folders = [f"infra/folder-{index:02d}" for index in range(6)]
    acquired = _wire_handler(monkeypatch, folders=folders, config=oversized)
    with pytest.raises(
        ConfigResolutionError,
        match="outer aggregate budget|folder configuration exceeds|credential lifetime",
    ):
        validate_and_resolve.handler(_handler_event(), object())
    assert acquired == []


def test_rendered_outer_map_item_selector_merges_shared_fields():
    source = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text(
        encoding="utf-8"
    )
    block = source.split("RunFolders = {", 1)[1].split("\n      }", 1)[0]
    assert "ItemSelector" in block
    for field in (
        "upstream_urls",
        "repo_name",
        "git_url",
        "commit_hash",
        "ssm_openci_tf_github_token",
        "ssm_infracost_api_key",
    ):
        assert f"$.map_shared.{field}" in block
    assert '"folder_config.$"           = "$$.Map.Item.Value.c"' in block
    assert '"account_binding.$"         = "$$.Map.Item.Value.b"' in block
    assert '"execution_id.$"            = "$$.Map.Item.Value.e"' in block


def test_credential_retry_merge_preserves_required_fields():
    shared = {
        "upstream_urls": _upstream_urls(),
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
    }
    compact = {
        "run_id": "r" * 32,
        "folder": "infra/a",
        "account_id": "123456789012",
        "b": [
            "openci-tf-executor-readonly",
            None,
            "openci-tf-0123456789abcdef",
            3600,
        ],
        "action": "plan",
        "attempt": 0,
        "budget": 3600,
        "deadline_at": "2099-01-01T00:00:00Z",
        "c": _maximum_folder_config(),
        "e": "r" * 32 + ".infra/a.0",
    }
    merged = merge_map_item(shared, compact)
    for field in (
        "budget",
        "folder_config",
        "upstream_urls",
        "git_url",
        "ssm_openci_tf_github_token",
        "ssm_infracost_api_key",
        "repo_name",
        "commit_hash",
    ):
        assert field in merged


@pytest.mark.parametrize(
    "marker",
    [
        {"trigger_id": "run", "status": "succeeded", "steps": [{}]},
        {"trigger_id": "run", "status": "succeeded", "steps": [{"name": "plan"}]},
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [_valid_step(output=None)],
        },
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [
                {
                    "step_name": "step-0",
                    "status": "succeeded",
                    "exit_code": 0,
                    "duration_seconds": {"not": "number"},
                    "output": ["not", "string"],
                }
            ],
        },
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [_valid_step(status="failed", exit_code=1, output="failed")],
        },
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [_valid_step(exit_code=7, output="")],
        },
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [_valid_step(output="", attacker="accepted")],
        },
    ],
)
def test_malformed_engine_markers_rejected(marker):
    with pytest.raises(MalformedResultError):
        parse_result(marker, "run")


def test_valid_succeeded_and_failed_markers_parse():
    succeeded = parse_result(_engine_marker("run"), "run")
    assert succeeded.succeeded
    failed = parse_result(
        _engine_marker("run", status="failed", exit_code=1, output="Error: boom"),
        "run",
    )
    assert not failed.succeeded
    assert failed.error == "Error: boom"


def test_codebuild_fallback_empty_steps_still_allowed():
    result = parse_result(
        {
            "trigger_id": "run",
            "status": "failed",
            "steps": [],
            "error": "codebuild_failed_without_result",
        },
        "run",
    )
    assert not result.succeeded
    assert result.steps == []


def test_poll_done_routes_malformed_marker_to_single_failure_manifest(monkeypatch):
    submitted_at = 1_700_000_000.0
    exec_id = "run.malformed.0"
    marker = _engine_marker(exec_id, status="succeeded", exit_code=7, output="")
    fresh_modified = datetime.fromtimestamp(submitted_at + 2, tz=timezone.utc)
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_: (marker, {"version_id": "v1", "last_modified": fresh_modified}),
    )
    with pytest.raises(MalformedResultError):
        poll_done.handler(
            {
                "exec_id": exec_id,
                "budget": 1,
                "deadline_at": "2099-01-01T00:00:00Z",
                "attempt": 0,
                "submitted_at": submitted_at,
                "done_baseline_version_id": None,
            },
            object(),
        )


def test_malformed_marker_failure_writer_emits_single_item(monkeypatch):
    persisted: list[dict] = []
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        write_failure_manifest, "put_json_create_only", lambda *_args, **_kwargs: "v1"
    )
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
            "failure_reason": "malformed done marker step exit_code",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "submitted_at": 1_700_000_000.0,
        },
        object(),
    )
    assert len(persisted) == 1
    assert summary["manifest_sha256"] == persisted[0]["manifest_sha256"]
    assert len(json.dumps(summary).encode()) < MAX_POLL_DONE_RESULT_BYTES


def test_bound_step_metadata_uses_real_step_name_only():
    steps = [
        _valid_step(status="failed", exit_code=1, output="x" * 1000),
    ]
    assert bound_step_metadata(steps) == [
        {"step_name": "step-0", "status": "failed", "exit_code": 1}
    ]


def test_credential_expiry_derived_before_output_stripped(monkeypatch):
    submitted_at = 1_700_000_000.0
    exec_id = "run.expire.0"
    marker = _engine_marker(
        exec_id,
        status="failed",
        exit_code=1,
        output="security token included in the request is expired",
    )
    fresh_modified = datetime.fromtimestamp(submitted_at + 2, tz=timezone.utc)
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_: (marker, {"version_id": "v1", "last_modified": fresh_modified}),
    )
    result = poll_done.handler(
        {
            "exec_id": exec_id,
            "budget": 1,
            "deadline_at": "2099-01-01T00:00:00Z",
            "attempt": 0,
            "submitted_at": submitted_at,
            "done_baseline_version_id": None,
        },
        object(),
    )
    assert result["credential_expired"]
    assert "output" not in result["steps"][0]


def test_rendered_asl_probe_routes_catchable_failures():
    from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition

    probe = load_rendered_run_folder_definition("read")["States"]["ProbeDone"]
    assert probe["ResultPath"] == "$.probe"
    assert probe["Catch"][1]["ErrorEquals"] == ["States.ALL"]
    assert probe["Catch"][1]["Next"] == "WriteFailureManifest"
