# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest  # type: ignore[import-not-found]

from src.core.errors import (
    MalformedResultError,
    PayloadTooLargeError,
    TriggerMismatchError,
)
from src.domain.engine.artifact_paths import (
    build_folder_artifact_keys,
    build_folder_artifact_keys_for_run,
    expected_plan_artifact_uris,
    manifest_key,
    pr_pointer_key,
)
from src.domain.engine.prepare import prepare_and_submit
from src.domain.engine.result import ExecutionResult, parse_result
from src.domain.formatters.artifacts import folder_comment
from src.services.run_folder import collect, poll_done
from src.services.run_folder import prepare_and_submit as prepare_handler
from tests.helpers.frozen_account import HUB_ACCOUNT_ID, apply_prepare_handler_env, frozen_account_fields
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition
from tests.unit.manifest_fixtures import complete_plan_object_mocks

_PREPARE_BINDING = frozen_account_fields(hub_account_id=HUB_ACCOUNT_ID)


def _artifact_head_meta(key: str, *, last_modified, body_size: int = 1) -> dict:
    if key.endswith(".zip"):
        content_type = "application/zip"
    elif key.endswith("/done"):
        content_type = "binary/octet-stream"
    elif key.endswith(".json"):
        content_type = "application/json"
        body_size = max(body_size, 2)
    else:
        content_type = "text/plain"
    return {
        "content_length": body_size,
        "content_type": content_type,
        "last_modified": last_modified,
        "checksum_sha256": "a" * 64,
    }


def _rendered_state_machine(lane: str = "read") -> dict[str, dict]:
    return load_rendered_run_folder_definition(lane)["States"]


def _state_next(state: dict) -> str:
    return str(state["Next"])


def test_prepare_encrypts_packages_uploads_then_submits_and_wipes_plaintext():
    calls = []

    def encrypt(path):
        calls.append(("encrypt", path))
        encrypted = f"{path}.enc"
        Path(encrypted).write_text("encrypted")
        return encrypted

    def package(path):
        calls.append(("package", path))
        assert Path(path).exists()
        return "archive.zip"

    prepare_and_submit(
        payload={"trigger_id": "run"},
        secrets={"token": "secret"},
        encrypt=encrypt,
        package=package,
        upload=lambda archive: calls.append(("upload", archive)),
        submit=lambda payload: calls.append(("submit", payload)),
    )

    assert [call[0] for call in calls] == ["encrypt", "package", "upload", "submit"]
    assert not Path(calls[0][1]).exists()


def test_prepare_wipes_plaintext_when_upload_fails():
    plaintext = []

    def encrypt(path):
        plaintext.append(path)
        return path

    with pytest.raises(RuntimeError, match="upload"):
        prepare_and_submit(
            payload={"trigger_id": "run"},
            secrets={},
            encrypt=encrypt,
            package=lambda _: "archive.zip",
            upload=lambda _: (_ for _ in ()).throw(RuntimeError("upload")),
            submit=Mock(),
        )
    assert not Path(plaintext[0]).exists()


def test_prepare_rejects_oversize_payload_before_adapters():
    adapter = Mock()
    with pytest.raises(PayloadTooLargeError):
        prepare_and_submit(
            payload={"body": "x" * 131_073},
            secrets={},
            encrypt=adapter,
            package=adapter,
            upload=adapter,
            submit=adapter,
        )
    adapter.assert_not_called()


def test_poll_done_deadline_returns_expired_without_s3_read(monkeypatch):
    read = Mock()
    monkeypatch.setattr(poll_done, "get_bounded_json_with_meta", read)
    result = poll_done.handler(
        {
            "exec_id": "run",
            "budget": 1,
            "deadline_at": "2000-01-01T00:00:00Z",
            "attempt": 0,
            "submitted_at": 1_700_000_000.0,
            "done_baseline_version_id": None,
        },
        object(),
    )
    assert result["probe_status"] == "expired"
    read.assert_not_called()


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


def test_poll_done_returns_parsed_marker(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    submitted_at = 1_700_000_000.0
    fresh_modified = __import__("datetime").datetime.fromtimestamp(
        submitted_at + 2, tz=__import__("datetime").timezone.utc
    )
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_: (
            {
                "trigger_id": "run",
                "status": "succeeded",
                "steps": [_valid_step()],
            },
            {"version_id": "v1", "last_modified": fresh_modified},
        ),
    )
    assert poll_done.handler(
        {
            "exec_id": "run",
            "budget": 1,
            "deadline_at": "2099-01-01T00:00:00Z",
            "attempt": 1,
            "submitted_at": submitted_at,
            "done_baseline_version_id": None,
        },
        object(),
    ) == {
        "exec_id": "run",
        "attempt": 1,
        "probe_status": "complete",
        "submitted_at": submitted_at,
        "succeeded": True,
        "error": None,
        "credential_expired": False,
        "steps": [{"step_name": "step-0", "status": "succeeded", "exit_code": 0}],
        "pointers": {"artifacts_prefix": "s3://tmp/run/", "done": "s3://done/run/done"},
    }


def test_done_marker_honors_engine_failed_status_without_error():
    marker = {
        "trigger_id": "run",
        "status": "failed",
        "steps": [
            {
                "step_name": "step-0",
                "status": "failed",
                "exit_code": 1,
                "duration_seconds": 1.0,
                "output": "ExpiredToken: credential expired",
            }
        ],
    }
    result = parse_result(marker, "run")
    assert not result.succeeded
    assert result.credential_expired
    assert result.error == "ExpiredToken: credential expired"


def test_credential_expiry_is_detected_only_from_failed_steps():
    result = parse_result(
        {
            "trigger_id": "run",
            "status": "failed",
            "steps": [
                {
                    "step_name": "step-0",
                    "status": "failed",
                    "output": "security token included in the request is expired",
                    "exit_code": 1,
                    "duration_seconds": 1.0,
                }
            ],
        },
        "run",
    )
    assert result.credential_expired


def test_successful_marker_with_ordinary_credential_output_is_not_expired():
    result = parse_result(
        {
            "trigger_id": "run",
            "status": "succeeded",
            "steps": [
                {
                    "step_name": "step-0",
                    "status": "succeeded",
                    "exit_code": 0,
                    "duration_seconds": 1.0,
                    "output": "credential rotation policy applied",
                }
            ],
        },
        "run",
    )
    assert not result.credential_expired


def test_done_marker_rejects_trigger_mismatch():
    with pytest.raises(TriggerMismatchError, match="trigger_id mismatch"):
        parse_result({"trigger_id": "other", "steps": [{}]}, "run")


@pytest.mark.parametrize(
    "marker, error",
    [
        ({"trigger_id": "run", "steps": []}, "malformed"),
        ({"trigger_id": "run", "status": "succeeded", "steps": "bad"}, "malformed"),
    ],
)
def test_done_marker_rejects_malformed_shapes(marker, error):
    with pytest.raises(MalformedResultError, match=error):
        parse_result(marker, "run")


def test_poll_done_ignores_abandoned_marker_then_returns_current_marker(monkeypatch):
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    submitted_at = 1_700_000_000.0
    fresh_modified = __import__("datetime").datetime.fromtimestamp(
        submitted_at + 2, tz=__import__("datetime").timezone.utc
    )
    markers = iter(
        (
            (
                {
                    "trigger_id": "abandoned",
                    "status": "failed",
                    "steps": [],
                    "error": "stale",
                },
                {"version_id": "v0", "last_modified": fresh_modified},
            ),
            (
                {
                    "trigger_id": "run",
                    "status": "succeeded",
                    "steps": [_valid_step()],
                },
                {"version_id": "v1", "last_modified": fresh_modified},
            ),
        )
    )
    monkeypatch.setattr(
        poll_done, "get_bounded_json_with_meta", lambda *_: next(markers)
    )
    event = {
        "exec_id": "run",
        "budget": 1,
        "deadline_at": "2099-01-01T00:00:00Z",
        "attempt": 0,
        "submitted_at": submitted_at,
        "done_baseline_version_id": None,
    }
    assert poll_done.handler(event, object())["probe_status"] == "pending"
    assert poll_done.handler(event, object())["succeeded"]


def test_codebuild_fallback_is_terminal_failure():
    result = parse_result(
        {
            "trigger_id": "run",
            "status": "failed",
            "steps": [],
            "error": "codebuild_failed_without_result",
        },
        "run",
    )
    assert result == ExecutionResult(
        "run", False, [], "codebuild_failed_without_result"
    )


def test_rendered_collect_does_not_reference_missing_plan_metadata_uri():
    collect = _rendered_state_machine()["Collect"]
    assert "plan_metadata_uri.$" not in collect["Parameters"]
    assert collect["Parameters"]["pointers.$"] == "$.probe.pointers"


def test_rendered_probe_receives_full_execution_context():
    probe = _rendered_state_machine()["ProbeDone"]
    assert "Parameters" not in probe
    assert probe["ResultPath"] == "$.probe"
    assert probe["Next"] == "RouteProbeOutcome"


def test_rendered_retry_task_preserves_full_lane_envelope():
    for lane in ("read", "apply", "destroy"):
        retry = _rendered_state_machine(lane)["BookkeepCredentialRetry"]
        assert retry["Parameters"]["event.$"] == "$"
        assert retry["ResultPath"] == "$"
        assert retry["Next"] == "PrepareAndSubmit"


def test_retry_choice_allows_only_attempt_zero_credential_expiry():
    choices = _rendered_state_machine()["RouteProbeOutcome"]["Choices"]
    probe_retry = next(
        rule
        for rule in choices
        if any(
            item.get("Variable") == "$.probe.credential_expired"
            for item in rule.get("And", [])
        )
    )
    predicates = probe_retry["And"]
    assert any(
        item.get("Variable") == "$.probe.succeeded"
        and item.get("BooleanEquals") is False
        for item in predicates
    )
    assert any(
        item.get("Variable") == "$.probe.credential_expired"
        and item.get("BooleanEquals") is True
        for item in predicates
    )
    assert any(
        item.get("Variable") == "$.probe.attempt"
        and item.get("NumericLessThan") == 1
        for item in predicates
    )
    assert probe_retry["Next"] == "BookkeepCredentialRetry"


def test_state_machine_follows_rendered_retry_transitions(monkeypatch, tmp_path):
    states = _rendered_state_machine()
    retry_choice = states["RouteProbeOutcome"]
    retry_next = next(
        rule
        for rule in retry_choice["Choices"]
        if any(
            item.get("Variable") == "$.probe.credential_expired"
            for item in rule.get("And", [])
        )
    )["Next"]
    default = retry_choice["Default"]

    def follow_retry(result):
        if (
            not result["succeeded"]
            and result["credential_expired"]
            and result["attempt"] < 1
        ):
            return retry_next
        return default

    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    apply_prepare_handler_env(monkeypatch)
    monkeypatch.setattr(
        prepare_handler.boto3,
        "Session",
        lambda: SimpleNamespace(get_credentials=lambda: None),
    )
    assumed_sessions = []
    monkeypatch.setattr(
        prepare_handler.sts,
        "assume_role",
        lambda *_, **kwargs: (
            assumed_sessions.append(kwargs["session_name"])
            or {"AWS_ACCESS_KEY_ID": "target"}
        ),
    )
    monkeypatch.setattr(
        prepare_handler.s3, "presign_get", lambda *args: f"get://{args[1]}"
    )
    monkeypatch.setattr(
        prepare_handler.s3, "presign_put", lambda *args, **_kwargs: f"put://{args[1]}"
    )
    monkeypatch.setattr(
        prepare_handler.s3,
        "presign_create_put",
        lambda *args: f"create-put://{args[1]}",
    )
    monkeypatch.setattr(prepare_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        prepare_handler, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path)
    )
    monkeypatch.setattr(prepare_handler, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_handler.sops, "encrypt_file", lambda path, _: path)
    monkeypatch.setattr(
        prepare_handler,
        "build_package",
        lambda *_args, **_kwargs: str(tmp_path / "package.zip"),
    )
    monkeypatch.setattr(
        prepare_handler.s3, "upload_file", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        prepare_handler.s3, "head_object", lambda *_args, **_kwargs: None
    )
    from src.domain.engine import prepare as prepare_domain

    monkeypatch.setattr(prepare_domain.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(prepare_handler.engine, "invoke_init_job", lambda *_: None)
    fresh_modified = __import__("datetime").datetime.fromtimestamp(
        1_700_000_010, tz=__import__("datetime").timezone.utc
    )
    marker_pairs = [
        (
            {
                "trigger_id": "run",
                "status": "failed",
                "steps": [
                    {
                        "step_name": "step-0",
                        "status": "failed",
                        "exit_code": 1,
                        "duration_seconds": 1.0,
                        "output": "ExpiredToken",
                    }
                ],
            },
            {"version_id": "v0", "last_modified": fresh_modified},
        ),
        (
            {
                "trigger_id": "run",
                "status": "succeeded",
                "steps": [_valid_step()],
            },
            {"version_id": "v1", "last_modified": fresh_modified},
        ),
    ]
    pair_iter = iter(marker_pairs)

    def fake_get(*_args, **_kwargs):
        key = _args[1]
        marker, meta = next(pair_iter)
        return {**marker, "trigger_id": key.rsplit("/", 1)[0]}, meta

    monkeypatch.setattr(poll_done, "get_bounded_json_with_meta", fake_get)

    state = {
        "action": "plan",
        "run_id": "run",
        "folder": "infra/a",
        "budget": 1,
        "deadline_at": "2099-01-01T00:00:00Z",
        "attempt": 0,
        "upstream_urls": {
            "tofu": "https://tofu",
            "tfsec": "https://tfsec",
            "infracost": "https://infracost",
        },
        "folder_config": {"account_alias": "target"},
        "git_url": "https://github.com/org/repo.git",
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
        "ssm_infracost_api_key": "",
        **_PREPARE_BINDING,
    }
    state["result"] = prepare_handler.handler(state, object())
    poll_input = {
        "exec_id": state["result"]["exec_id"],
        "budget": state["budget"],
        "deadline_at": "2099-01-01T00:00:00Z",
        "attempt": state["result"]["attempt"],
        "submitted_at": state["result"]["submitted_at"],
        "done_baseline_version_id": state["result"]["done_baseline_version_id"],
        "plan_metadata_uri": state["result"]["plan_metadata_uri"],
    }
    first_plan_metadata_uri = state["result"]["plan_metadata_uri"]
    state["result"] = poll_done.handler(poll_input, object())
    assert state["result"]["credential_expired"] and not state["result"]["succeeded"]
    assert _state_next(states["ProbeDone"]) == "RouteProbeOutcome"
    assert follow_retry(state["result"]) == "BookkeepCredentialRetry"
    assert _state_next(states["BookkeepCredentialRetry"]) == "PrepareAndSubmit"
    retry_state = {
        key: state[key]
        for key in (
            "action",
            "run_id",
            "folder",
            "budget",
            "deadline_at",
            "upstream_urls",
            "folder_config",
            "git_url",
            "commit_hash",
            "ssm_openci_tf_github_token",
            "ssm_infracost_api_key",
            "repo_name",
            "account_id",
            "account_binding",
        )
    }
    retry_state["attempt"] = state["result"]["attempt"] + 1
    retry_result = prepare_handler.handler(retry_state, object())
    assert retry_result["exec_id"] != poll_input["exec_id"]
    assert retry_result["attempt"] == 1
    assert retry_result["plan_metadata_uri"] == first_plan_metadata_uri
    assert (
        "openci-tf/org/repo/run/infra/a/tf/plan-metadata.json" in first_plan_metadata_uri
    )
    assert len(assumed_sessions) == 2
    state["result"] = poll_done.handler(
        {
            "exec_id": retry_result["exec_id"],
            "budget": 1,
            "deadline_at": "2099-01-01T00:00:00Z",
            "attempt": 1,
            "submitted_at": retry_result["submitted_at"],
            "done_baseline_version_id": retry_result["done_baseline_version_id"],
            "plan_metadata_uri": retry_result["plan_metadata_uri"],
        },
        object(),
    )
    assert (
        state["result"]["pointers"]["plan_metadata"]
        == retry_result["plan_metadata_uri"]
    )
    collect_input = {
        **{
            key: state["result"][key]
            for key in ("exec_id", "attempt", "succeeded", "steps", "error", "pointers")
        },
        "action": state["action"],
        "repo_name": state["repo_name"],
        "commit_hash": state["commit_hash"],
        "folder": state["folder"],
        "run_id": "run",
        "account_id": "123456789012",
        "submitted_at": state["result"]["submitted_at"],
        "plan_metadata_uri": state["result"].get("plan_metadata_uri"),
    }
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    monkeypatch.setattr(collect, "put_folder_attempt", lambda **_kwargs: None)
    _lm = __import__("datetime").datetime(
        2026, 8, 10, tzinfo=__import__("datetime").timezone.utc
    )
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=state["result"]["exec_id"],
        repo_name=state["repo_name"],
        run_id="run",
        commit_hash=state["commit_hash"],
        account_id="123456789012",
        folder=state["folder"],
        attempt=state["result"]["attempt"],
        last_modified=_lm,
    )
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(
        collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata
    )
    collected = collect.handler(collect_input, object())
    assert collected["succeeded"] is True
    assert (
        collected["pointers"]["done"] == f"s3://done/{state['result']['exec_id']}/done"
    )
    keys = build_folder_artifact_keys(
        repo_name=state["repo_name"], run_id="run", folder_path=state["folder"]
    )
    assert collected["pointers"]["artifacts_prefix"] == f"s3://tmp/{keys.prefix}"
    assert (
        collected["manifest_s3_uri"]
        == f"s3://tmp/{manifest_key(state['repo_name'], 'run', state['folder'])}"
    )
    assert collected["exec_id"] == state["result"]["exec_id"]


def test_collect_publishes_plan_pointer_when_pr_context_exists(monkeypatch):
    repo_name = "org/repo"
    run_id = "1700000000000.deadbeef"
    folder = "infra/a"
    exec_id = "run.abc.0"
    keys = build_folder_artifact_keys_for_run(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=7,
        pointer_type="plan",
    )
    published: list[tuple[str, str]] = []
    committed_manifest: dict = {}

    def capture_publish(**kwargs):
        published.append((kwargs["key"], kwargs["execution_id"]))

    def capture_manifest(_bucket: str, _key: str, manifest: dict) -> str:
        committed_manifest.update(manifest)
        return "v1"

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(collect, "put_json_create_only", capture_manifest)
    monkeypatch.setattr(collect, "publish_execution_pointer", capture_publish)
    monkeypatch.setattr(collect, "put_folder_attempt", lambda **_kwargs: None)
    monkeypatch.setattr(
        collect,
        "resolve_run_artifact_layout",
        lambda **_kwargs: __import__(
            "src.domain.engine.run_artifact_layout", fromlist=["RunArtifactLayout"]
        ).RunArtifactLayout(
            folder_keys=keys,
            pr_number=7,
            pointer_type="plan",
        ),
    )
    _lm = __import__("datetime").datetime(
        2026, 8, 10, tzinfo=__import__("datetime").timezone.utc
    )
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id,
        repo_name=repo_name,
        run_id=run_id,
        commit_hash="a" * 40,
        account_id="123456789012",
        folder=folder,
        attempt=0,
        last_modified=_lm,
        pr_number=7,
        pointer_type="plan",
    )

    def head_without_drift(bucket: str, key: str):
        if key == keys.drift_json:
            return None
        return head_object(bucket, key)

    monkeypatch.setattr(collect, "head_object", head_without_drift)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(
        collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata
    )
    expected = expected_plan_artifact_uris(
        bucket="tmp",
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=7,
        pointer_type="plan",
    )
    collected = collect.handler(
        {
            "exec_id": exec_id,
            "succeeded": True,
            "steps": [],
            "pointers": {
                "done": f"s3://done/{exec_id}/done",
                "plan_metadata": expected.metadata,
            },
            "action": "plan",
            "repo_name": repo_name,
            "commit_hash": "a" * 40,
            "folder": folder,
            "account_id": "123456789012",
            "run_id": run_id,
            "attempt": 0,
            "plan_metadata_uri": expected.metadata,
        },
        object(),
    )
    assert collected["succeeded"] is True
    assert published == [
        (
            pr_pointer_key(
                repo_name=repo_name,
                pr_number=7,
                folder_path=folder,
                pointer_type="plan",
            ),
            run_id,
        )
    ]
    assert committed_manifest
    assert committed_manifest["manifest_s3_uri"] == (
        f"s3://tmp/{manifest_key(repo_name, run_id, folder, pr_number=7, pointer_type='plan')}"
    )


def test_prepare_uploads_execution_scoped_package_key(monkeypatch, tmp_path):
    uploads: list[tuple[str, str]] = []
    submitted: list[dict] = []
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    apply_prepare_handler_env(monkeypatch)
    monkeypatch.setattr(
        prepare_handler.boto3,
        "Session",
        lambda: SimpleNamespace(get_credentials=lambda: None),
    )
    apply_prepare_handler_env(monkeypatch)
    monkeypatch.setattr(
        prepare_handler.sts,
        "assume_role",
        lambda *_, **kwargs: {"AWS_ACCESS_KEY_ID": "target"},
    )
    monkeypatch.setattr(
        prepare_handler.s3, "presign_get", lambda *args: f"get://{args[1]}"
    )
    monkeypatch.setattr(
        prepare_handler.s3, "presign_put", lambda *args, **_kwargs: f"put://{args[1]}"
    )
    monkeypatch.setattr(
        prepare_handler.s3,
        "presign_create_put",
        lambda *args: f"create-put://{args[1]}",
    )
    monkeypatch.setattr(prepare_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        prepare_handler, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path)
    )
    monkeypatch.setattr(prepare_handler, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_handler.sops, "encrypt_file", lambda path, _: path)
    monkeypatch.setattr(
        prepare_handler,
        "build_package",
        lambda *_args, **_kwargs: str(tmp_path / "package.zip"),
    )
    monkeypatch.setattr(
        prepare_handler.s3,
        "upload_file",
        lambda archive, bucket, key, **_kwargs: uploads.append((bucket, key)),
    )
    monkeypatch.setattr(
        prepare_handler.s3, "head_object", lambda *_args, **_kwargs: None
    )
    from src.domain.engine import prepare as prepare_domain

    monkeypatch.setattr(prepare_domain.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(
        prepare_handler.engine,
        "invoke_init_job",
        lambda *_args, **_kwargs: submitted.append(_args[1]) or None,
    )
    base = {
        "action": "plan",
        "run_id": "run-a",
        "folder": "infra/a",
        "budget": 60,
        "deadline_at": "2099-01-01T00:00:00Z",
        "attempt": 0,
        "upstream_urls": {
            "tofu:1.8.0": "https://tofu",
            "tfsec:1.28.10": "https://tfsec",
            "infracost:0.10.39": "https://infracost",
        },
        "folder_config": {"account_alias": "target", "tf_runtime": "tofu:1.8.0"},
        "git_url": "https://github.com/org/repo.git",
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
        "ssm_infracost_api_key": "",
        **_PREPARE_BINDING,
    }
    first = prepare_handler.handler({**base, "run_id": "run-a"}, object())
    second = prepare_handler.handler(
        {**base, "run_id": "run-b", "folder": "infra/b"}, object()
    )
    assert uploads[0][1] == f"{first['exec_id']}.zip"
    assert uploads[1][1] == f"{second['exec_id']}.zip"
    assert uploads[0][1] != uploads[1][1]
    assert submitted[0]["s3_package_uri"].endswith(f"/{first['exec_id']}.zip")
    assert submitted[1]["s3_package_uri"].endswith(f"/{second['exec_id']}.zip")
    assert "tfsec" not in submitted[0]["s3_package_uri"]


def test_collect_output_is_bounded_and_keeps_pointers_only(monkeypatch):
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    _lm = __import__("datetime").datetime(
        2026, 8, 10, tzinfo=__import__("datetime").timezone.utc
    )
    _lm = __import__("datetime").datetime(2026, 8, 10, tzinfo=__import__("datetime").timezone.utc)
    drift_body = b'{"drift":true}'

    def drift_head(_bucket, key):
        metadata = _artifact_head_meta(
            key,
            last_modified=_lm,
            body_size=len(drift_body) if key.endswith("drift.json") else 1,
        )
        if key.endswith("drift.json"):
            metadata["checksum_sha256"] = __import__("hashlib").sha256(drift_body).hexdigest()
        return metadata

    monkeypatch.setattr(collect, "head_object", drift_head)
    monkeypatch.setattr(
        collect,
        "get_object_bytes",
        lambda _bucket, key, _limit: drift_body if key.endswith("drift.json") else b"x",
    )
    output = collect.handler(
        {
            "exec_id": "run",
            "succeeded": False,
            "error": "failed",
            "pointers": {"log": "s3://bucket/log", "done": "s3://done/run/done"},
            "action": "drift",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "folder": "infra/a",
            "account_id": "123456789012",
            "run_id": "run",
        },
        object(),
    )
    assert output["succeeded"] is False
    assert output["error"] == "failed"
    assert output["drift_detected"] is True
    assert "manifest_s3_uri" in output
    from src.domain.engine.summary import validate_outer_child_output

    validate_outer_child_output(
        output,
        folder="infra/a",
        account_id="123456789012",
        execution_id="run",
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"drift": True}, True), ({"drift": False}, False)],
)
def test_collect_reads_authoritative_drift_result(payload, expected, monkeypatch):
    body = __import__("json").dumps(payload, separators=(",", ":")).encode()
    manifest = {
        "entries": [
            {
                "name": "drift.json",
                "s3_uri": "s3://tmp/openci-tf/org/repo/run/infra/drift.json",
                "checksum": __import__("hashlib").sha256(body).hexdigest(),
            }
        ]
    }
    monkeypatch.setattr(collect, "get_object_bytes", lambda *_args, **_kwargs: body)

    assert collect._drift_result(manifest, action="drift", tmp_bucket="tmp") is expected


@pytest.mark.parametrize("body", [None, b"{", b"{}", b'{"drift":1}', b'{"drift":false,"extra":true}'])
def test_collect_rejects_missing_or_malformed_drift_result(body, monkeypatch):
    checksum = __import__("hashlib").sha256(body or b"").hexdigest()
    manifest = {
        "entries": [
            {
                "name": "drift.json",
                "s3_uri": "s3://tmp/openci-tf/org/repo/run/infra/drift.json",
                "checksum": checksum,
            }
        ]
    }
    monkeypatch.setattr(collect, "get_object_bytes", lambda *_args, **_kwargs: body)

    with pytest.raises(ValueError, match="drift result"):
        collect._drift_result(manifest, action="drift", tmp_bucket="tmp")


def test_collect_old_or_failed_drift_without_result_is_unknown():
    assert collect._drift_result({"entries": []}, action="drift", tmp_bucket="tmp") is None
    assert collect._drift_result({"entries": []}, action="plan", tmp_bucket="tmp") is None


def test_collect_rejects_complete_child_output_over_budget():
    from src.core.errors import ConfigResolutionError
    from src.domain.engine.summary import validate_outer_child_output

    oversized = {
        "exec_id": "run",
        "succeeded": True,
        "pointers": {"log": "x" * 4_500},
    }
    with pytest.raises(ConfigResolutionError, match="outer map outcome exceeds"):
        validate_outer_child_output(
            oversized,
            folder="infra/a",
            account_id="123456789012",
            execution_id="run",
        )


def test_collect_passes_credential_expiry_from_poll_shape_to_renderer(monkeypatch):
    collect_parameters = _rendered_state_machine()["Collect"]["Parameters"]
    assert collect_parameters["credential_expired.$"] == "$.probe.credential_expired"
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    _lm = __import__("datetime").datetime(
        2026, 8, 10, tzinfo=__import__("datetime").timezone.utc
    )
    poll_done_result = {
        "exec_id": "run",
        "succeeded": False,
        "credential_expired": True,
        "steps": [],
        "error": "ExpiredToken",
        "pointers": {},
        "action": "plan",
        "run_id": "run",
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "account_id": "123456789012",
        "folder": "infra/a",
        "attempt": 0,
    }
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: "v1")
    monkeypatch.setattr(
        collect,
        "head_object",
        lambda _bucket, key: _artifact_head_meta(key, last_modified=_lm),
    )
    monkeypatch.setattr(collect, "get_object_bytes", lambda *_args, **_kwargs: b"x")
    monkeypatch.setattr(collect, "get_bounded_json", lambda *_args, **_kwargs: None)
    outcome = {
        **collect.handler(poll_done_result, object()),
        "account_id": "123456789012",
    }
    assert outcome["credential_expired"] is True
    assert "Credentials expired" in folder_comment("infra/a", outcome, {})


def test_rendered_failure_paths_route_to_write_failure_manifest():
    states = _rendered_state_machine()
    assert states["ValidateAction"]["Default"] == "WriteFailureManifest"
    assert states["RouteProbeOutcome"]["Default"] == "WriteFailureManifest"
    for task_name in ("PrepareAndSubmit", "ProbeDone", "Collect"):
        assert any(
            catcher["Next"] == "WriteFailureManifest"
            for catcher in states[task_name]["Catch"]
        )
    assert _state_next(states["WriteFailureManifest"]) == "FolderExecutionFailed"
    assert states["FolderExecutionFailed"]["Type"] == "Fail"


def test_write_failure_manifest_persists_registry_and_returns_bounded_summary(
    monkeypatch,
):
    from src.services.run_folder import write_failure_manifest

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
            "failure_reason": "prepare failed",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "submitted_at": 1_700_000_000.0,
        },
        object(),
    )
    assert summary["succeeded"] is False
    assert summary["manifest_s3_uri"].startswith("s3://tmp/")
    assert summary["manifest_sha256"]
    assert persisted[0]["manifest_sha256"] == summary["manifest_sha256"]
    assert persisted[0]["execution_id"].endswith(".0")


def test_every_presignable_artifact_content_type_matches_engine_uploader():
    """Live failure: apply.out presign signed octet-stream while the engine sent
    text/plain, so the upload 403'd and collect failed on a missing artifact.
    The presign content-type table must agree with the uploader's rule
    (*.out => text/plain, *.json => application/json, else octet-stream)."""
    from src.domain.engine.artifact_paths import build_folder_artifact_keys
    from src.platform.aws.s3 import _content_type_for_key
    from src.services.run_folder.prepare_and_submit import _artifact_names

    names = set()
    for action in ("plan", "report", "drift", "plan_destroy", "apply", "destroy"):
        names.update(_artifact_names(action))
    # Sidecars uploaded by the engine helper scripts with explicit content types
    # (live failure: destroy-plan-metadata.json signed octet-stream vs sent json).
    keys = build_folder_artifact_keys(repo_name="o/r", run_id="a" * 32, folder_path="f")
    for field in ("plan_metadata", "destroy_plan_metadata"):
        names.add(getattr(keys, field).rsplit("/", 1)[-1])
    for field in (
        "plan_sha256",
        "destroy_plan_sha256",
        "plan_tfplan",
        "destroy_plan_tfplan",
    ):
        if hasattr(keys, field):
            names.add(getattr(keys, field).rsplit("/", 1)[-1])
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if base.endswith((".out", ".sha256")):
            expected = "text/plain"
        elif base.endswith(".json"):
            expected = "application/json"
        elif base.endswith(".tfplan"):
            expected = "application/octet-stream"
        else:
            expected = "application/octet-stream"
        assert _content_type_for_key(name) == expected, name
