# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production-shaped acceptance tests for audit remediation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest  # type: ignore[import-not-found]

from src.domain.engine.artifact_limits import (
    MAX_PACKAGE_BYTES,
    MAX_RAW_ARTIFACT_BYTES,
)
from src.domain.engine.lifecycle import (
    conservative_api_expiry_iso,
    s3_lifecycle_expiration_utc,
)
from src.domain.engine.manifest import (
    BucketSet,
    ManifestBinding,
    build_manifest,
    validate_manifest_binding,
    validate_manifest_schema,
)
from src.domain.run.api_authorization import ApiAuthorizationError, _load_policies
from src.domain.run.folder_id import decode_folder_id, encode_folder_id
from src.domain.run.outcome import normalize_map_outcome
from src.platform.aws.run_registry.keys import (
    folder_attempt_sk,
    folder_summary_sk,
)
from src.platform.aws import dynamo, s3
from src.services.orchestration import finalize_run
from src.services.run_folder import collect
from src.domain.engine.artifact_paths import manifest_key
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition
from tests.unit.manifest_fixtures import (
    committed_success_plan_manifest,
    complete_plan_object_mocks,
)

def _manifest_uri(run: str, folder: str) -> str:
    return f"s3://tmp/{manifest_key('org/repo', run, folder)}"


def _rendered_state_machine(lane: str = "read") -> dict[str, dict]:
    return load_rendered_run_folder_definition(lane)["States"]


def test_collect_asl_passes_binding_dimensions():
    collect_parameters = _rendered_state_machine()["Collect"]["Parameters"]
    for field in (
        "run_id",
        "repo_name",
        "commit_hash",
        "account_id",
        "folder",
        "attempt",
        "exec_id",
    ):
        assert f"{field}.$" in collect_parameters


def test_failure_manifest_task_receives_raw_state_and_execution_start():
    states = _rendered_state_machine()
    assert states["WriteFailureManifest"]["Parameters"] == {
        "event.$": "$",
        "execution_started_at.$": "$$.Execution.StartTime",
    }
    for state_name in (
        "NormalizePrepareFailure",
        "NormalizeProbeFailure",
        "NormalizeCollectFailure",
    ):
        assert state_name not in states


def test_caught_credential_expiry_bookkeeps_evidence_before_retry():
    states = _rendered_state_machine()
    prepare_catch = states["PrepareAndSubmit"]["Catch"][0]
    assert prepare_catch["Next"] == "RouteProbeOutcome"
    route_choices = states["RouteProbeOutcome"]["Choices"]
    caught_retry = next(
        rule
        for rule in route_choices
        if any(
            item.get("Variable") == "$.error.Error"
            and item.get("StringEquals") == "CredentialExpiredError"
            for item in rule.get("And", [])
        )
    )
    assert caught_retry["Next"] == "BookkeepCredentialRetry"
    assert states["BookkeepCredentialRetry"]["Next"] == "PrepareAndSubmit"


def _artifact_head_meta(
    key: str, *, last_modified: datetime, body_size: int = 1
) -> dict:
    if key.endswith(".zip"):
        content_type = "application/zip"
    elif key.endswith((".json", "/done")):
        content_type = (
            "binary/octet-stream" if key.endswith("/done") else "application/json"
        )
    else:
        content_type = "text/plain"
    return {
        "content_length": body_size,
        "content_type": content_type,
        "last_modified": last_modified,
        "checksum_sha256": "a" * 64,
    }


def test_manifest_collect_binding_validation_accepts_complete_manifest(monkeypatch):
    last_modified = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    captured: dict = {}
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id="run.abc.0",
        repo_name="org/repo",
        run_id="run123",
        commit_hash="b" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        last_modified=last_modified,
        package_body=b"z" * 70_000,
    )

    def fake_put(_bucket, _key, manifest):
        captured.update(manifest)
        return "v1"

    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(
        collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata
    )
    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "put_json_create_only", fake_put)
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    collect.handler(
        {
            "exec_id": "run.abc.0",
            "attempt": 0,
            "succeeded": True,
            "credential_expired": False,
            "steps": [],
            "error": None,
            "pointers": {"done": "s3://done/run.abc.0/done"},
            "action": "plan",
            "repo_name": "org/repo",
            "commit_hash": "b" * 40,
            "account_id": "123456789012",
            "folder": "infra/a",
            "run_id": "run123",
            "submitted_at": 1_700_000_000.0,
            "plan_metadata_uri": plan_metadata["metadata_s3_uri"],
        },
        object(),
    )
    validate_manifest_schema(captured, execution_id="run.abc.0")
    validate_manifest_binding(
        captured,
        run_id="run123",
        repo_name="org/repo",
        commit_hash="b" * 40,
        account_id="123456789012",
        folder="infra/a",
        action="plan",
        attempt=0,
    )


def test_package_checksum_allows_70_kib():
    last_modified = datetime(2026, 8, 10, tzinfo=timezone.utc)
    package_body = b"z" * 70_000
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id="run.abc.0",
        repo_name="org/repo",
        run_id="run123",
        commit_hash="c" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        last_modified=last_modified,
        package_body=package_body,
    )

    manifest = build_manifest(
        execution_id="run.abc.0",
        buckets=BucketSet(
            tmp_bucket="tmp",
            done_bucket="done",
            package_bucket="pkg",
            done_uri="s3://done/run.abc.0/done",
            package_uri="s3://pkg/run.abc.0.zip",
        ),
        binding=ManifestBinding(
            run_id="run123",
            repo_name="org/repo",
            commit_hash="c" * 40,
            account_id="123456789012",
            folder="infra/a",
            attempt=0,
        ),
        action="plan",
        head_object=head_object,
        read_object_bytes=read_object_bytes,
        plan_metadata=plan_metadata,
        plan_dimensions={
            "repo_name": "org/repo",
            "commit_hash": "c" * 40,
            "account_id": "123456789012",
            "folder": "infra/a",
            "attempt": 0,
        },
        generated_at_source=last_modified,
    )
    package_entry = next(
        entry for entry in manifest["entries"] if entry["name"] == "package"
    )
    assert package_entry["size"] == 70_000


def test_raw_artifact_checksum_allows_200_kib():
    last_modified = datetime(2026, 8, 10, tzinfo=timezone.utc)
    body = b"x" * 200_000

    def head_object(_bucket, key):
        if key.endswith(".zip"):
            content_type = "application/zip"
        elif key.endswith(("drift.json", "/done")):
            content_type = (
                "binary/octet-stream" if key.endswith("/done") else "application/json"
            )
        else:
            content_type = "text/plain"
        return {
            "content_length": len(body),
            "content_type": content_type,
            "last_modified": last_modified,
            "checksum_sha256": hashlib.sha256(body).hexdigest(),
        }

    def read_object_bytes(_bucket, _key, max_bytes):
        assert max_bytes == MAX_RAW_ARTIFACT_BYTES
        return body

    manifest = build_manifest(
        execution_id="run.abc.0",
        buckets=BucketSet(
            tmp_bucket="tmp",
            done_bucket="done",
            package_bucket="pkg",
            done_uri="s3://done/run.abc.0/done",
            package_uri="s3://pkg/run.abc.0.zip",
        ),
        binding=ManifestBinding(
            run_id="run123",
            repo_name="org/repo",
            commit_hash="c" * 40,
            account_id="123456789012",
            folder="infra/a",
            attempt=0,
        ),
        action="drift",
        head_object=head_object,
        read_object_bytes=read_object_bytes,
        plan_metadata=None,
        generated_at_source=last_modified,
    )
    assert any(
        entry["name"] == "init.out" and entry["size"] == 200_000
        for entry in manifest["entries"]
    )


def test_s3_checksum_decodes_base64_head_metadata(monkeypatch):
    body = b"package"
    digest = hashlib.sha256(body).digest()
    client = Mock()
    client.head_object.return_value = {
        "ContentLength": len(body),
        "LastModified": datetime(2026, 8, 7, tzinfo=timezone.utc),
        "ChecksumSHA256": base64.b64encode(digest).decode("ascii"),
    }
    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: client)
    meta = s3.head_object("pkg", "run.zip")
    assert meta is not None
    assert meta["checksum_sha256"] == hashlib.sha256(body).hexdigest()


def test_folder_opaque_keys_do_not_collide_on_delimiters():
    left = "infra#attempt#0000"
    right = "infra"
    assert folder_summary_sk(left) != folder_attempt_sk(right, 0)


def test_multibyte_folder_round_trip_at_utf8_byte_bound():
    folder = "界" * 64
    assert len(folder.encode("utf-8")) == 192
    folder_id = encode_folder_id(folder)
    assert decode_folder_id(folder_id) == folder


def test_finalizer_normalizes_nested_map_output():
    normalized = normalize_map_outcome(
        {
            "folder": "infra/a",
            "execution_id": "outer",
            "account_id": "123456789012",
            "output": {
                "exec_id": "inner",
                "attempt": 1,
                "status": "succeeded",
                "manifest_s3_uri": "s3://tmp/inner/manifest.json",
                "manifest_sha256": "d" * 64,
            },
        }
    )
    assert normalized["execution_id"] == "inner"
    assert normalized["attempt"] == 1
    assert normalized["manifest_sha256"] == "d" * 64


def test_finalizer_continues_after_one_folder_failure(monkeypatch):
    calls: list[str] = []

    def fake_put(**kwargs):
        calls.append(kwargs["folder"])
        if kwargs["folder"] == "bad":
            raise RuntimeError("write failed")

    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(finalize_run, "get_folder_attempt", lambda *_args: None)
    monkeypatch.setattr(finalize_run, "put_folder_record", fake_put)
    monkeypatch.setattr(
        finalize_run, "finalize_run_if_running", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])
    with pytest.raises(RuntimeError, match="bad: write failed"):
        finalize_run.handler(
            {
                "run_id": "run",
                "outcomes": [
                    {
                        "folder": "bad",
                        "execution_id": "e1",
                        "account_id": "123456789012",
                        "output": {"status": "failed"},
                    },
                    {
                        "folder": "good",
                        "execution_id": "e2",
                        "account_id": "123456789012",
                        "output": {"status": "succeeded"},
                    },
                ],
            },
            object(),
        )
    assert calls == ["bad", "good"]


def test_finalizer_persists_synthetic_config_failure_execution_id(monkeypatch):
    persisted: list[dict[str, object]] = []
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(finalize_run, "get_folder_attempt", lambda *_args: None)
    monkeypatch.setattr(
        finalize_run, "put_folder_record", lambda **kwargs: persisted.append(kwargs)
    )
    monkeypatch.setattr(
        finalize_run, "finalize_run_if_running", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])

    result = finalize_run.handler(
        {
            "run_id": "run-config-failure",
            "outcomes": [
                {
                    "folder": "config",
                    "status": "infrastructure_error",
                    "error": "configuration resolution failed",
                }
            ],
        },
        object(),
    )

    assert result == {"finalized": True}
    assert persisted[0]["execution_id"] == "config-run-config-failure"
    assert persisted[0]["status"] == "infrastructure_error"


def test_finalizer_accepts_matching_authoritative_attempt_without_replay(monkeypatch):
    replay = Mock()
    finalized: list[tuple[str, str]] = []
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        finalize_run,
        "get_folder_attempt",
        lambda *_args: {"execution_id": "run.folder.0", "status": "failed"},
    )
    monkeypatch.setattr(finalize_run, "put_folder_record", replay)
    monkeypatch.setattr(
        finalize_run,
        "finalize_run_if_running",
        lambda run_id, status: finalized.append((run_id, status)),
    )
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])

    result = finalize_run.handler(
        {
            "run_id": "run",
            "outcomes": [
                {
                    "folder": "infra/a",
                    "execution_id": "run.folder.0",
                    "attempt": 0,
                    "status": "failed",
                }
            ],
        },
        object(),
    )

    assert result == {"finalized": True}
    replay.assert_not_called()
    assert finalized == [("run", "failed")]


def test_finalizer_rehydrates_compact_map_items_without_execution_id_mismatch(monkeypatch):
    replay = Mock()
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        finalize_run,
        "get_folder_attempt",
        lambda *_args: {
            "execution_id": "1787618846778.538c5fed.f445281ac67b.0",
            "status": "failed",
        },
    )
    monkeypatch.setattr(finalize_run, "put_folder_record", replay)
    monkeypatch.setattr(
        finalize_run, "finalize_run_if_running", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])

    result = finalize_run.handler(
        {
            "run_id": "1787618846778.538c5fed",
            "map_shared": {
                "upstream_urls": {"tofu:1.10.6": "https://example.com/tofu"},
                "repo_name": "org/repo",
                "git_url": "https://github.com/org/repo.git",
                "commit_hash": "a" * 40,
                "ssm_openci_tf_github_token": "/openci-tf/github/token",
                "ssm_infracost_api_key": "/openci-tf/infracost/key",
            },
            "map_items": [
                {
                    "run_id": "1787618846778.538c5fed",
                    "folder": "terraform/primary/ap-northeast-1/06-sns-topic",
                    "account_id": "123456789012",
                    "action": "destroy",
                    "attempt": 0,
                    "budget": 3600,
                    "deadline_at": "2099-01-01T00:00:00Z",
                    "b": [
                        "openci-tf-executor-poweruser",
                        None,
                        "openci-tf-0123456789abcdef",
                        3600,
                    ],
                    "c": {"account_alias": "target", "tf_runtime": "tofu:1.10.6"},
                    "e": "1787618846778.538c5fed.f445281ac67b.0",
                }
            ],
            "outcomes": [],
        },
        object(),
    )

    assert result == {"finalized": True}
    replay.assert_not_called()


def test_finalizer_still_reports_persisted_execution_id_mismatch(monkeypatch):
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        finalize_run,
        "get_folder_attempt",
        lambda *_args: {"execution_id": "run.folder.0", "status": "failed"},
    )
    monkeypatch.setattr(finalize_run, "put_folder_record", Mock())
    monkeypatch.setattr(
        finalize_run, "finalize_run_if_running", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])

    with pytest.raises(RuntimeError, match="persisted attempt execution id mismatch"):
        finalize_run.handler(
            {
                "run_id": "run",
                "outcomes": [
                    {
                        "folder": "infra/a",
                        "execution_id": "different.folder.0",
                        "attempt": 0,
                        "status": "failed",
                    }
                ],
            },
            object(),
        )


def test_eventbridge_terminal_event_finalizes_registry_run(monkeypatch):
    finalized: list[tuple[str, str]] = []
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        finalize_run,
        "finalize_run_if_running",
        lambda run_id, status: finalized.append((run_id, status)),
    )
    result = finalize_run.handler(
        {
            "detail": {
                "status": "FAILED",
                "executionArn": "arn:aws:states:us-east-1:123456789012:execution:openci-tf:run-event",
            }
        },
        object(),
    )
    assert result == {"finalized": True}
    assert finalized == [("run-event", "failed")]


def test_eventbridge_terminal_event_rejects_unsafe_execution_name(monkeypatch):
    finalized = Mock()
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(finalize_run, "finalize_run_if_running", finalized)
    assert finalize_run.handler(
        {"detail": {"executionArn": "arn:aws:states:x:execution:openci-tf:../../run"}},
        object(),
    ) == {"finalized": True}
    finalized.assert_not_called()


def test_policy_parser_rejects_noncanonical_types(monkeypatch):
    monkeypatch.setenv(
        "API_CALLER_POLICY_JSON",
        json.dumps(
            {
                "arn:aws:iam::123456789012:role/reader": {
                    "trigger_ids": ["repo"],
                    "actions": ["plan"],
                    "artifact_classes": ["manifest"],
                    "binary_plan": "true",
                }
            }
        ),
    )
    with pytest.raises(ApiAuthorizationError, match="boolean"):
        _load_policies()


def test_manifest_entry_schema_rejects_unknown_fields():
    manifest = {
        "version": 1,
        "execution_id": "run.abc.0",
        "action": "plan",
        "generated_at": "2026-08-10T12:00:00Z",
        "manifest_s3_uri": _manifest_uri("run", "infra/a"),
        "entries": [
            {
                "name": "done",
                "s3_uri": "s3://done/run.abc.0/done",
                "content_type": "application/json",
                "size": 1,
                "checksum": "a" * 64,
                "expires_at": "2026-08-11T00:00:00Z",
                "extra": True,
            }
        ],
        "package_bucket": "pkg",
        "tmp_bucket": "tmp",
        "done_bucket": "done",
        "plan_retention_days": 1,
        "run_id": "run",
        "repo_name": "org/repo",
        "commit_hash": "c" * 40,
        "account_id": "123456789012",
        "folder": "infra/a",
        "attempt": 0,
        "failure_reason": "terminal failure",
        "manifest_sha256": "b" * 64,
    }
    with pytest.raises(ValueError, match="unknown fields"):
        validate_manifest_schema(manifest)


def test_lifecycle_expiry_uses_s3_day_boundary():
    modified = datetime(2026, 8, 10, 15, 30, tzinfo=timezone.utc)
    expiry = s3_lifecycle_expiration_utc(modified, 3)
    assert expiry == datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
    assert conservative_api_expiry_iso(modified, 3) == "2026-08-13T00:00:00Z"


def test_package_iam_denies_nested_zip_paths():
    iam = (
        Path(__file__).parents[2] / "infra/deploy/modules/run_folder/iam.tf"
    ).read_text()
    assert "package_nested_zip_deny" in iam
    assert "${var.package_bucket_arn}/*/*.zip" in iam
    assert "${var.package_bucket_arn}/*.zip" in iam
    assert "package_execution_zip_condition" not in iam


def test_poll_done_preserves_submitted_at_for_collect_transition(monkeypatch):
    from src.services.run_folder import poll_done

    submitted_at = 1_700_000_000.0
    fresh_modified = datetime.fromtimestamp(submitted_at + 2, tz=timezone.utc)
    marker = {
        "trigger_id": "run",
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
    meta = {"version_id": "v1", "last_modified": fresh_modified}
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(
        poll_done,
        "get_bounded_json_with_meta",
        lambda *_args, **_kwargs: (marker, meta),
    )
    result = poll_done.handler(
        {
            "exec_id": "run",
            "budget": 1,
            "deadline_at": "2099-01-01T00:00:00Z",
            "attempt": 0,
            "submitted_at": submitted_at,
            "done_baseline_version_id": None,
        },
        object(),
    )
    assert result["submitted_at"] == submitted_at
    collect_parameters = _rendered_state_machine()["Collect"]["Parameters"]
    assert collect_parameters["submitted_at.$"] == "$.probe.submitted_at"


def test_failure_manifest_bytes_are_deterministic():
    from src.domain.engine.manifest import build_failure_manifest

    source = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    kwargs = {
        "execution_id": "run.abc.0",
        "tmp_bucket": "tmp",
        "done_bucket": "done",
        "package_bucket": "pkg",
        "action": "plan",
        "failure_reason": "boom",
        "run_id": "run",
        "repo_name": "org/repo",
        "commit_hash": "c" * 40,
        "account_id": "123456789012",
        "folder": "infra/a",
        "attempt": 0,
        "generated_at_source": source,
    }
    assert build_failure_manifest(**kwargs) == build_failure_manifest(**kwargs)


def test_writer_then_persister_use_byte_equivalent_outcome(monkeypatch):
    from src.services.run_folder import persist_retry_attempt, write_failure_manifest

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
    monkeypatch.setattr(
        persist_retry_attempt,
        "put_folder_attempt",
        lambda **kwargs: persisted.append(kwargs),
    )
    event = {
        "run_id": "run",
        "folder": "infra/a",
        "action": "plan",
        "account_id": "123456789012",
        "attempt": 0,
        "exec_id": "run.abc.0",
        "repo_name": "org/repo",
        "commit_hash": "c" * 40,
        "submitted_at": 1_700_000_000.0,
        "credential_expired": True,
        "failure_reason": "credential expired before retry",
        "result": {"attempt": 0, "exec_id": "run.abc.0"},
    }
    retry_manifest = write_failure_manifest.handler(event, object())
    persist_retry_attempt.handler({**event, "retry_manifest": retry_manifest}, object())
    assert len(persisted) == 2
    assert persisted[0]["outcome"] == persisted[1]["outcome"]


def test_write_failure_manifest_reconciles_existing_success_manifest(monkeypatch):
    from src.domain.engine.execution_id import compose_execution_id
    from src.services.run_folder import write_failure_manifest

    exec_id = compose_execution_id("run", "infra/a", 0)
    success_manifest = committed_success_plan_manifest(
        execution_id=exec_id,
        commit_hash="c" * 40,
    )

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    monkeypatch.setattr(
        write_failure_manifest,
        "get_bounded_json",
        lambda *_args, **_kwargs: success_manifest,
    )
    monkeypatch.setattr(
        write_failure_manifest, "put_folder_attempt", lambda **_kwargs: None
    )
    summary = write_failure_manifest.handler(
        {
            "run_id": "run",
            "folder": "infra/a",
            "action": "plan",
            "account_id": "123456789012",
            "attempt": 0,
            "repo_name": "org/repo",
            "commit_hash": "c" * 40,
            "submitted_at": 1_700_000_000.0,
            "failure_reason": "late failure",
        },
        object(),
    )
    assert summary["manifest_sha256"] == success_manifest["manifest_sha256"]


def test_collect_role_has_tmp_kms_write_permissions():
    iam = (
        Path(__file__).parents[2] / "infra/deploy/modules/run_folder/iam.tf"
    ).read_text()
    collect_block = iam.split('resource "aws_iam_role_policy" "collect"')[1].split(
        "resource "
    )[0]
    assert "kms:GenerateDataKey" in collect_block
    assert "kms:Encrypt" in collect_block
    assert "dynamodb:TransactWriteItems" in collect_block


def test_collect_lambda_wires_registry_table_name():
    lambdas = (
        Path(__file__).parents[2] / "infra/deploy/modules/run_folder/lambdas.tf"
    ).read_text()
    assert (
        'each.key == "persist-retry-attempt" || each.key == "write-failure-manifest" || each.key == "collect"'
        in lambdas
    )


def test_non_default_done_lifecycle_wires_through_deploy():
    deploy_vars = (Path(__file__).parents[2] / "infra/deploy/variables.tf").read_text()
    deploy_main = (Path(__file__).parents[2] / "infra/deploy/main.tf").read_text()
    assert 'variable "done_lifecycle_days"' in deploy_vars
    assert "done_lifecycle_days        = var.done_lifecycle_days" in deploy_main
    assert (
        conservative_api_expiry_iso(datetime(2026, 8, 10, tzinfo=timezone.utc), 180)
        == "2027-02-06T00:00:00Z"
    )


def test_nfc_equivalent_folders_collapse_in_request():
    from src.domain.run.request import parse_run_request

    first = parse_run_request(
        {
            "trigger_id": "trigger-1",
            "commit_hash": "a" * 40,
            "action": "plan",
            "folder_mode": "explicit",
            "folders": ["e\u0301"],
            "idempotency_key": "key-12345678",
        }
    )
    second = parse_run_request(
        {
            "trigger_id": "trigger-1",
            "commit_hash": "a" * 40,
            "action": "plan",
            "folder_mode": "explicit",
            "folders": ["\u00e9", "e\u0301"],
            "idempotency_key": "key-12345678",
        }
    )
    assert first.folders == ["é"]
    assert second.folders == ["é"]


def test_finalizer_persists_missing_map_outcomes(monkeypatch):
    calls: list[str] = []

    def fake_put(**kwargs):
        calls.append(kwargs["folder"])

    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(finalize_run, "get_folder_attempt", lambda *_args: None)
    monkeypatch.setattr(finalize_run, "put_folder_record", fake_put)
    monkeypatch.setattr(
        finalize_run, "finalize_run_if_running", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(finalize_run, "_release_locks", lambda *_args, **_kwargs: [])
    finalize_run.handler(
        {
            "run_id": "run",
            "map_items": [{"folder": "infra/missing", "account_id": "123456789012"}],
            "outcomes": [],
        },
        object(),
    )
    assert "infra/missing" in calls


def test_render_defers_registry_terminalization_until_after_github(monkeypatch):
    from types import SimpleNamespace

    from src.services.render import handler as render_handler

    updates: list[tuple[str, str]] = []
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render_handler, "_uses_github_pr", lambda _event: True)
    monkeypatch.setattr(render_handler, "get_github_token", lambda _path: "token")
    monkeypatch.setattr(
        render_handler, "list_text_prefix", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        render_handler, "_delete_and_repost", lambda *_args, **_kwargs: 1
    )
    monkeypatch.setattr(
        render_handler, "_delete_generated_comment", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        render_handler,
        "_delete_transient_status_comment",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        render_handler.boto3,
        "resource",
        lambda *_args, **_kwargs: SimpleNamespace(Table=lambda *_a, **_k: object()),
    )
    monkeypatch.setattr(
        render_handler.run_lock, "release", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        render_handler,
        "_update_run_registry",
        lambda event, outcomes, action, *, skipped=None: updates.append(
            (action, str(len(outcomes)))
        ),
    )
    render_handler.handler(
        {
            "run_id": "run",
            "action": "drift",
            "webhook_info": {
                "repo_name": "org/repo",
                "pr_number": 1,
                "commit_hash": "a" * 40,
            },
            "settings": {"ssm_openci_tf_github_token": "/token"},
            "outcomes": [
                {
                    "folder": "infra/a",
                    "execution_id": "e1",
                    "account_id": "123456789012",
                    "status": "succeeded",
                    "succeeded": True,
                }
            ],
        },
        object(),
    )
    assert updates == [("drift", "1")]


def test_package_upload_rejects_over_50_mib(monkeypatch, tmp_path):
    from src.services.run_folder import prepare_and_submit as prepare_handler

    archive = tmp_path / "package.zip"
    archive.write_bytes(b"z" * (MAX_PACKAGE_BYTES + 1))
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    with pytest.raises(ValueError, match="package exceeds"):
        prepare_handler._upload_bounded_package(str(archive), "packages", "run.zip")


def test_incomplete_manifest_schema_rejected_even_with_digest():
    manifest = {
        "version": 1,
        "execution_id": "run.abc.0",
        "action": "plan",
        "generated_at": "2026-08-10T12:00:00Z",
        "entries": [],
        "manifest_sha256": "b" * 64,
        "run_id": "run",
        "repo_name": "org/repo",
        "commit_hash": "c" * 40,
        "account_id": "123456789012",
        "folder": "infra/a",
        "attempt": 0,
        "failure_reason": "failed",
    }
    with pytest.raises(ValueError, match="missing fields"):
        validate_manifest_schema(manifest)


def test_unsafe_action_routes_raw_binding_fields_to_manifest_writer():
    states = _rendered_state_machine()
    assert states["ValidateAction"]["Default"] == "WriteFailureManifest"
    assert "RejectUnsafeAction" not in states


def test_credential_retry_task_preserves_prepare_inputs_as_one_typed_envelope():
    task = _rendered_state_machine()["BookkeepCredentialRetry"]
    assert task["Parameters"] == {
        "event.$": "$",
        "execution_started_at.$": "$$.Execution.StartTime",
    }
    assert task["ResultPath"] == "$"


def test_collect_reconciles_committed_success_on_failed_event(monkeypatch):
    from src.domain.engine.execution_id import compose_execution_id

    exec_id = compose_execution_id("run", "infra/a", 0)
    committed = committed_success_plan_manifest(
        execution_id=exec_id, commit_hash="c" * 40, run_id="run"
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

    def fake_get_bounded_json(_bucket, key, _limit):
        if key.endswith("manifest.json"):
            return committed
        return plan_metadata

    monkeypatch.setattr(collect, "get_bounded_json", fake_get_bounded_json)
    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(
        collect,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    summary = collect.handler(
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
    assert summary["succeeded"] is True
    assert summary["manifest_sha256"] == committed["manifest_sha256"]


def test_discover_folder_paths_rejects_canonical_collision(tmp_path, monkeypatch):
    from pathlib import Path

    from src.core.errors import ConfigResolutionError
    from src.domain.config.outer_state import discover_folder_paths

    root = Path(tmp_path / "repo")
    root.mkdir()

    class FakeConfigPath:
        def __init__(self, folder_name: str):
            self._folder = root / folder_name

        @property
        def parent(self):
            class OpenciTf:
                parent = self._folder

            return OpenciTf()

    def fake_rglob(self, pattern):
        if self.resolve() != root.resolve():
            return Path.rglob(self, pattern)
        return [FakeConfigPath("\u00e9"), FakeConfigPath("e\u0301")]

    monkeypatch.setattr(Path, "rglob", fake_rglob)
    with pytest.raises(ConfigResolutionError, match="canonical folder collision"):
        discover_folder_paths(root)


def test_discover_folder_paths_maps_nfd_physical_to_nfc_key(tmp_path):
    from src.domain.config.outer_state import discover_folder_paths, resolve_outer_state

    root = tmp_path / "repo"
    folder = root / "e\u0301"
    (folder / ".openci_tf").mkdir(parents=True)
    (folder / ".openci_tf" / "config.yaml").write_text(
        "account_alias: target\ntf_runtime: tofu:1.10.6\n"
    )
    paths = discover_folder_paths(root)
    assert list(paths.keys()) == ["é"]
    assert paths["é"] == "e\u0301"
    resolved = resolve_outer_state(
        str(root), ["é"], {"tofu:1.10.6": "https://example.com/tofu"}, "drift"
    )
    assert "é" in resolved["folder_configs"]


def test_justfile_passes_lifecycle_variables_to_foundation_and_deploy():
    justfile = (Path(__file__).parents[2] / "justfile").read_text().replace(" ", "")
    for key in (
        "tmp_lifecycle_days",
        "package_lifecycle_days",
        "done_lifecycle_days",
        "plan_retention_days",
    ):
        assert f"get-or{key}" in justfile
    assert "tmp_lifecycle_days=${TMP_LIFECYCLE_DAYS}" in justfile
    assert "plan_retention_days=${PLAN_RETENTION_DAYS}" in justfile


def test_engine_install_script_targets_adjacent_engine_tree():
    script = (Path(__file__).parents[2] / "scripts/engine_install.sh").read_text()
    assert "build-release-zip.sh" in script
    assert "infra/01-ecr" in script
    assert "infra/02-deploy" in script
    assert "--query KeyMetadata.Arn" in script
    assert 'ECR_CANONICAL_STATE_KEY="engine-ecr/terraform.tfstate"' in script
    state_key = (Path(__file__).parents[2] / "scripts/engine_state_key.sh").read_text()
    assert 'CANONICAL_STATE_KEY="engine/terraform.tfstate"' in state_key
    assert 'LEGACY_STATE_KEY="engine-02-deploy/terraform.tfstate"' in state_key
    assert 'generate_backend.sh" "$STATE_BUCKET" engine-ecr' in script
    assert "mirror-image.sh" in script
    assert 'rev-parse --short=7 HEAD' in script
    assert "engine_image_uri" in script
    assert 'tofu -chdir="$ECR_DIR" output -raw repository_url' in script
    assert "generate_tfvars.sh" in script
    assert "init -migrate-state -force-copy -input=false" in script
    assert "state list)" in script
    assert 'delete-object --bucket "$STATE_BUCKET" --key "$LEGACY_STATE_KEY"' in script
    # Decision 27: no DynamoDB lock/checksum rows remain; S3 native lock file.
    assert "delete_checksum_row" not in script
    assert "dynamodb" not in script
    assert "both canonical and legacy engine state objects exist" in script
    assert (
        'upload_source.sh" "$STATE_BUCKET" engine "$ENGINE_ROOT" infra/02-deploy'
        in script
    )


def test_engine_install_and_uninstall_choose_the_engine_state_key_in_one_place():
    scripts = Path(__file__).parents[2] / "scripts"
    for name in ("engine_install.sh", "engine_uninstall.sh"):
        script = (scripts / name).read_text()
        assert 'source "${ROOT_DIR}/scripts/engine_state_key.sh"' in script, name
        assert 'state_key_choice="$(engine_state_key_choice "$STATE_BUCKET")"' in script, name
        assert "engine-02-deploy/terraform.tfstate" not in script, name
        assert "state_object_status()" not in script, name


def test_engine_uninstall_script_reverses_ecr_then_deploy():
    script = (Path(__file__).parents[2] / "scripts/engine_uninstall.sh").read_text()
    deploy_index = script.index('generate_backend.sh" "$STATE_BUCKET" engine')
    ecr_index = script.index('generate_backend.sh" "$STATE_BUCKET" engine-ecr')
    assert deploy_index < ecr_index
    assert "infra/02-deploy" in script
    assert "infra/01-ecr" in script
    assert "tofu destroy -input=false -auto-approve" in script
    assert "batch-delete-image" not in script


def _run_engine_uninstall(
    tmp_path: Path,
    *,
    tfvars: bool,
    deploy_state: str,
    legacy_state: str = "absent",
    ecr_state: str = "absent",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the real engine_uninstall.sh with aws, terraform and tofu stubbed.

    Each state is what head-object and get-object return for one key:
    deploy_state for engine/terraform.tfstate, legacy_state for the legacy
    engine-02-deploy/terraform.tfstate, ecr_state for engine-ecr/terraform.tfstate.
    "absent" (404 / NoSuchKey), "empty" (destroyed state), "resources",
    "denied" (head-object works, get-object AccessDenied, as when KMS decrypt is
    denied), or "forbidden" (head-object 403 too).
    """
    repo_root = Path(__file__).parents[2]
    engine = tmp_path / "engine"
    for root in ("infra/02-deploy", "infra/01-ecr"):
        (engine / root).mkdir(parents=True)
        if tfvars:
            (engine / root / "terraform.tfvars").write_text('aws_region = "us-east-1"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    aws = """[ "$*" != "sts get-caller-identity --query Account --output text" ] || { echo 123456789012; exit 0; }
key=""; prev=""
for arg in "$@"; do [ "$prev" != --key ] || key="$arg"; prev="$arg"; done
case "$key" in
  engine/terraform.tfstate) state=@DEPLOY@ ;;
  engine-02-deploy/terraform.tfstate) state=@LEGACY@ ;;
  engine-ecr/terraform.tfstate) state=@ECR@ ;;
  *) exit 99 ;;
esac
case "$1 $2:$state" in
  "s3api head-object:absent") echo "An error occurred (404) when calling the HeadObject operation: Not Found" >&2; exit 254 ;;
  "s3api head-object:forbidden") echo "An error occurred (403) when calling the HeadObject operation: Forbidden" >&2; exit 254 ;;
  "s3api head-object:"*) echo '{"ContentLength": 100}' ;;
  "s3api get-object:absent") echo "An error occurred (NoSuchKey) when calling the GetObject operation: The specified key does not exist." >&2; exit 254 ;;
  "s3api get-object:denied"|"s3api get-object:forbidden") echo "An error occurred (AccessDenied) when calling the GetObject operation: Access Denied" >&2; exit 254 ;;
  "s3api get-object:empty") echo '{"version": 4, "resources": []}' > "${@: -1}" ;;
  "s3api get-object:resources") echo '{"version": 4, "resources": [{"mode": "managed", "type": "aws_sfn_state_machine", "name": "x"}]}' > "${@: -1}" ;;
  *) exit 99 ;;
esac
"""
    stubs = {
        "aws": aws.replace("@DEPLOY@", deploy_state)
        .replace("@LEGACY@", legacy_state)
        .replace("@ECR@", ecr_state),
        "terraform": "",
        "tofu": "",
    }
    for name, text in stubs.items():
        (bin_dir / name).write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{calls}"\n{text}')
        (bin_dir / name).chmod(0o755)
    completed = subprocess.run(
        ["bash", str(repo_root / "scripts/engine_uninstall.sh"), str(engine)],
        cwd=tmp_path,
        env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "OPENCI_TF_PROJECT": "openci-tf"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, calls.read_text().splitlines() if calls.exists() else []


def _terraform_calls(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith(("terraform ", "tofu "))]


@pytest.mark.parametrize("deploy_state", ["absent", "empty"])
def test_engine_uninstall_without_tfvars_or_engine_resources_says_not_installed(tmp_path, deploy_state):
    completed, calls = _run_engine_uninstall(tmp_path, tfvars=False, deploy_state=deploy_state)
    assert completed.returncode == 0, completed.stderr
    assert not any(" destroy " in f"{call} " for call in calls)
    assert "the engine is not installed, nothing to destroy" in completed.stdout
    assert "engine uninstall: the engine is not installed; nothing was destroyed" in completed.stdout
    assert "engine uninstall complete" not in completed.stdout


def test_engine_uninstall_without_tfvars_but_with_engine_resources_fails_loud(tmp_path):
    """An installed engine whose tfvars file is gone is a destroy that did not run."""
    completed, calls = _run_engine_uninstall(tmp_path, tfvars=False, deploy_state="resources")
    assert completed.returncode == 1, completed.stderr
    assert not any(" destroy " in f"{call} " for call in calls)
    assert "infra/02-deploy/terraform.tfvars is missing" in completed.stderr
    assert "still tracks 1 engine resource(s); the engine was NOT destroyed" in completed.stderr
    assert "'just engine'" in completed.stderr
    assert "'just engine-destroy'" in completed.stderr
    assert "engine uninstall complete" not in completed.stdout


def test_engine_uninstall_without_tfvars_and_unreadable_state_fails_loud(tmp_path):
    completed, calls = _run_engine_uninstall(tmp_path, tfvars=False, deploy_state="denied")
    assert completed.returncode != 0
    assert not any(" destroy " in f"{call} " for call in calls)
    assert "AccessDenied" in completed.stderr
    assert "could not read engine state s3://openci-tf-state-123456789012/engine/terraform.tfstate" in completed.stderr
    assert "engine uninstall complete" not in completed.stdout


@pytest.mark.parametrize("deploy_state", ["resources", "absent"])
def test_engine_uninstall_with_tfvars_destroys_both_roots(tmp_path, deploy_state):
    completed, calls = _run_engine_uninstall(tmp_path, tfvars=True, deploy_state=deploy_state)
    assert completed.returncode == 0, completed.stderr
    assert "terraform destroy -input=false -auto-approve" in calls
    assert "tofu destroy -input=false -auto-approve" in calls
    assert not any("get-object" in call for call in calls)
    assert "engine uninstall complete" in completed.stdout


@pytest.mark.parametrize("tfvars", [True, False])
def test_engine_uninstall_refuses_an_engine_under_the_legacy_state_key(tmp_path, tfvars):
    """engine_install.sh still accepts engine-02-deploy/terraform.tfstate; the uninstall
    never destroys the empty canonical key or calls that engine absent."""
    completed, calls = _run_engine_uninstall(
        tmp_path, tfvars=tfvars, deploy_state="absent", legacy_state="resources"
    )
    assert completed.returncode == 1, completed.stderr
    assert _terraform_calls(calls) == []
    assert (
        "s3://openci-tf-state-123456789012/engine-02-deploy/terraform.tfstate"
        " (the legacy engine state key) still tracks 1 engine resource(s)" in completed.stderr
    )
    assert "the engine was NOT destroyed" in completed.stderr
    assert "'just engine'" in completed.stderr
    assert "'just engine-destroy'" in completed.stderr
    assert "not installed" not in completed.stdout
    assert "engine uninstall complete" not in completed.stdout


def test_engine_uninstall_with_an_emptied_legacy_state_object_says_not_installed(tmp_path):
    """A legacy object that tracks no resources is no engine: the uninstall goes on."""
    completed, calls = _run_engine_uninstall(
        tmp_path, tfvars=False, deploy_state="absent", legacy_state="empty"
    )
    assert completed.returncode == 0, completed.stderr
    assert _terraform_calls(calls) == []
    assert "engine uninstall: the engine is not installed; nothing was destroyed" in completed.stdout


@pytest.mark.parametrize("tfvars", [True, False])
def test_engine_uninstall_refuses_when_both_engine_state_keys_exist(tmp_path, tfvars):
    completed, calls = _run_engine_uninstall(
        tmp_path, tfvars=tfvars, deploy_state="resources", legacy_state="resources"
    )
    assert completed.returncode == 1, completed.stderr
    assert _terraform_calls(calls) == []
    assert "both s3://openci-tf-state-123456789012/engine/terraform.tfstate and" in completed.stderr
    assert "the engine was NOT destroyed" in completed.stderr
    assert "engine uninstall complete" not in completed.stdout


def test_engine_uninstall_fails_loud_when_the_state_key_probe_fails(tmp_path):
    """A 403 on head-object never falls through to the canonical key."""
    completed, calls = _run_engine_uninstall(tmp_path, tfvars=True, deploy_state="forbidden")
    assert completed.returncode != 0
    assert _terraform_calls(calls) == []
    assert "Forbidden" in completed.stderr
    assert (
        "could not determine state object status for"
        " s3://openci-tf-state-123456789012/engine/terraform.tfstate" in completed.stderr
    )
    assert "engine uninstall complete" not in completed.stdout


def test_engine_uninstall_without_tfvars_but_with_ecr_resources_fails_loud(tmp_path):
    completed, calls = _run_engine_uninstall(
        tmp_path, tfvars=False, deploy_state="absent", ecr_state="resources"
    )
    assert completed.returncode == 1, completed.stderr
    assert _terraform_calls(calls) == []
    assert "infra/01-ecr/terraform.tfvars is missing" in completed.stderr
    assert "engine-ecr/terraform.tfstate still tracks 1 engine resource(s)" in completed.stderr


def test_verify_uses_canonical_engine_source_and_bucket_names():
    script = (Path(__file__).parents[2] / "scripts/verify.sh").read_text()
    assert "bootstrap foundation deploy engine" in script
    assert "target-connect/terraform.tfstate" in script
    assert "source copy target-connect" in script
    assert "bootstrap foundation deploy target-connect engine" not in script
    assert "bootstrap foundation deploy target-connect engine-02-deploy" not in script
    assert 'for b in internal "done"; do' in script
    assert "engine bucket ${PROJECT}-${b}-${ACCOUNT_ID}" not in script
    assert "account_bucket_exists.sh" in script


def test_account_bucket_exists_reports_present_absent_and_list_errors(tmp_path: Path):
    repo_root = Path(__file__).parents[2]
    helper = repo_root / "scripts/account_bucket_exists.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def write_fake_aws(mode: str) -> None:
        (bin_dir / "aws").write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"list-buckets"*)
    case "{mode}" in
      present) echo "openci-tf-internal" ;;
      absent) echo "" ;;
      error)
        echo "AccessDenied: blocked" >&2
        exit 254
        ;;
    esac
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 99
    ;;
esac
"""
        )
        (bin_dir / "aws").chmod(0o755)

    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    write_fake_aws("present")
    present = subprocess.run(
        ["bash", "-c", f'source "{helper}"; account_bucket_exists openci-tf-internal'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert present.returncode == 0, present.stderr

    write_fake_aws("absent")
    absent = subprocess.run(
        ["bash", "-c", f'source "{helper}"; account_bucket_exists openci-tf-internal'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert absent.returncode == 1
    assert "not in this account" in absent.stderr

    write_fake_aws("error")
    error = subprocess.run(
        ["bash", "-c", f'source "{helper}"; account_bucket_exists openci-tf-internal'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert error.returncode == 254
    assert "AccessDenied" in error.stderr


def test_upload_source_excludes_untracked_and_value_bearing_files(tmp_path):
    root = tmp_path / "repo"
    deploy = root / "infra" / "deploy"
    deploy.mkdir(parents=True)
    tracked = {
        "main.tf": 'resource "null_resource" "main" {}\n',
        "variables.tf": 'variable "token" { type = string }\n',
        ".terraform.lock.hcl": "# provider hashes only\n",
        "backend.tf": 'terraform { backend "s3" {} }\n',
        "override.tf": "locals { override = true }\n",
        "terraform.tfvars": 'token = "must-not-upload"\n',
    }
    for name, content in tracked.items():
        (deploy / name).write_text(content)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", "."], check=True)
    (deploy / "secrets.tf").write_text('locals { secret = "untracked" }\n')

    capture = tmp_path / "capture"
    capture.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        '[ "$1" = s3 ] && [ "$2" = sync ]\n'
        'cp -R "$3/." "$CAPTURE/"\n'
    )
    (bin_dir / "terraform").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        '[ "$1" = version ] && [ "$2" = -json ]\n'
        "printf '%s\\n' '{\"terraform_version\":\"1.14.0\"}'\n"
    )
    (bin_dir / "aws").chmod(0o755)
    (bin_dir / "terraform").chmod(0o755)
    env = os.environ | {
        "CAPTURE": str(capture),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    script = Path(__file__).parents[2] / "scripts" / "upload_source.sh"
    subprocess.run(
        ["bash", str(script), "state-bucket", "engine", str(root), "infra/deploy"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    uploaded = {
        path.relative_to(capture).as_posix()
        for path in capture.rglob("*")
        if path.is_file()
    }
    assert uploaded == {
        "infra/deploy/main.tf",
        "infra/deploy/variables.tf",
        "infra/deploy/.terraform.lock.hcl",
        "manifest.json",
    }
    manifest = json.loads((capture / "manifest.json").read_text())
    assert manifest["variable_names"] == ["token"]


def test_upload_source_rejects_escaping_roots_and_git_failures(tmp_path):
    root = tmp_path / "repo"
    deploy = root / "infra" / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "main.tf").write_text("locals { safe = true }\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "infra/deploy/main.tf"], check=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "terraform.tfvars").write_text('external_api_key = "must-not-read"\n')
    (root / "outside-link").symlink_to(outside, target_is_directory=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_called = tmp_path / "aws-called"
    (bin_dir / "aws").write_text('#!/bin/sh\nset -eu\n: > "$AWS_CALLED"\n')
    (bin_dir / "aws").chmod(0o755)
    script = Path(__file__).parents[2] / "scripts" / "upload_source.sh"
    env = os.environ | {
        "AWS_CALLED": str(aws_called),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    for rel in ("../outside", "outside-link"):
        aws_called.unlink(missing_ok=True)
        completed = subprocess.run(
            ["bash", str(script), "state-bucket", "escape", str(root), rel],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert not aws_called.exists()

    real_git = shutil.which("git")
    assert real_git is not None
    (bin_dir / "git").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'case " $* " in *" ls-files "*) exit 42 ;; esac\n'
        'exec "$REAL_GIT" "$@"\n'
    )
    (bin_dir / "git").chmod(0o755)
    aws_called.unlink(missing_ok=True)
    completed = subprocess.run(
        ["bash", str(script), "state-bucket", "git-failure", str(root), "infra/deploy"],
        env=env | {"REAL_GIT": real_git},
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert "could not enumerate tracked source" in completed.stderr
    assert not aws_called.exists()


def test_get_repo_settings_strips_webhook_secret_trailing_whitespace(monkeypatch):
    item = {
        "sk": "trigger",
        "repo_name": "org/repo",
        "git_url": "https://example.invalid/org/repo.git",
        "webhook_secret_ssm": "/openci-tf/webhook/secret",
    }

    class Table:
        def get_item(self, *, Key):
            assert Key == {"pk": "repo", "sk": "trigger"}
            return {"Item": item}

    monkeypatch.setattr(dynamo, "_table", lambda _: Table())
    monkeypatch.setattr(
        dynamo,
        "get_parameter",
        lambda path, **_kwargs: f"  webhook-secret-value\n",
    )
    settings = dynamo.get_repo_settings("trigger")
    assert settings.secret == "webhook-secret-value"


def test_api_repository_lookup_does_not_decrypt_webhook_secret(monkeypatch):
    item = {
        "sk": "trigger",
        "repo_name": "org/repo",
        "git_url": "https://example.invalid/org/repo.git",
        "webhook_secret_ssm": "/openci-tf/webhook/secret",
    }

    class Table:
        def get_item(self, *, Key):
            assert Key == {"pk": "repo", "sk": "trigger"}
            return {"Item": item}

    monkeypatch.setattr(dynamo, "_table", lambda _: Table())
    get_parameter = Mock(side_effect=AssertionError("webhook secret must not be read"))
    monkeypatch.setattr(dynamo, "get_parameter", get_parameter)
    settings = dynamo.get_repo_settings("trigger", with_webhook_secret=False)
    assert settings.secret == ""
    get_parameter.assert_not_called()
    decrypt = Mock(return_value="webhook-secret")
    monkeypatch.setattr(dynamo, "get_parameter", decrypt)
    webhook_settings = dynamo.get_repo_settings("trigger")
    assert webhook_settings.secret == "webhook-secret"
    decrypt.assert_called_once_with("/openci-tf/webhook/secret")
    source = (
        Path(__file__).parents[2] / "src/services/orchestration/start_run.py"
    ).read_text()
    assert "get_repo_settings(request.trigger_id, with_webhook_secret=False)" in source


def test_cleanup_operator_footprint_deletes_product_log_groups_and_operator_ssm():
    repo_root = Path(__file__).parents[2]
    cleanup = (repo_root / "scripts/cleanup_operator_footprint.sh").read_text()
    log_groups = (repo_root / "scripts/product_log_groups.sh").read_text()
    assert "product_log_groups.sh" in cleanup
    assert "product_log_group_names" in cleanup
    for token in (
        "init-job",
        "worker",
        "finalizer",
        "/aws/codebuild/",
        "/aws/vendedlogs/states/",
        "run-folder",
    ):
        assert token in log_groups
    for prefix in (
        "/openci-tf/clone-token",
        "/openci-tf/env",
        "/openci-tf/infracost",
        "/openci-tf/webhook",
    ):
        assert prefix in cleanup
    assert "delete_legacy_role_if_exists" in cleanup
    assert "executor-local" in cleanup
    assert "executor-remote" in cleanup


def test_justfile_uninstall_and_bootstrap_destroy_invoke_operator_cleanup():
    justfile = (Path(__file__).parents[2] / "justfile").read_text()
    uninstall = justfile.split("uninstall:", 1)[1].split("verify:", 1)[0]
    bootstrap = justfile.split("bootstrap-destroy:", 1)[1].split("foundation:", 1)[0]
    assert "./scripts/cleanup_operator_footprint.sh" in uninstall
    assert "./scripts/cleanup_operator_footprint.sh" in bootstrap


def _run_uninstall_journey(
    tmp_path: Path, *, fail_recipe: str = "", fail_ssm_project: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run the real uninstall recipe body with just, aws, and the phase scripts stubbed."""
    repo_root = Path(__file__).parents[2]
    justfile = (repo_root / "justfile").read_text()
    recipe = justfile.split("\nuninstall:", 1)[1].split("\nverify:", 1)[0]
    body = "\n".join(line[4:] for line in recipe.splitlines() if line.startswith("    "))
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(repo_root / "scripts/phase_timing.sh", work / "scripts/phase_timing.sh")
    stubs = {
        bin_dir / "aws": "#!/usr/bin/env bash\necho 123456789012\n",
        bin_dir / "just": (
            '#!/usr/bin/env bash\necho "$1" >> just-calls\n'
            f'[ "$1" != "{fail_recipe}" ] || exit 3\n'
        ),
        work / "scripts/poweruser_needs_destroy.sh": "#!/usr/bin/env bash\nexit 1\n",
        work / "scripts/ssm_config.sh": (
            '#!/usr/bin/env bash\necho "$SSM_CONFIG_PROJECT" >> ssm-delete-all\n'
            f'[ "$SSM_CONFIG_PROJECT" != "{fail_ssm_project}" ] || exit 4\n'
        ),
        work / "scripts/cleanup_operator_footprint.sh": "#!/usr/bin/env bash\ntouch operator-cleanup-ran\n",
    }
    for path, text in stubs.items():
        path.write_text(text)
        path.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OPENCI_TF_KEEP_STATE": "no",
        "SSM_CONFIG_PROJECT": "openci-tf",
    }
    return subprocess.run(
        ["bash", "-c", body],
        cwd=work,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _lines(path: Path) -> list[str]:
    return path.read_text().split() if path.exists() else []


def test_uninstall_runs_every_phase_in_order(tmp_path):
    completed = _run_uninstall_journey(tmp_path)
    work = tmp_path / "work"
    assert completed.returncode == 0, completed.stderr
    assert _lines(work / "just-calls") == [
        "deploy-destroy",
        "engine-destroy",
        "foundation-destroy",
        "bootstrap-destroy",
    ]
    assert _lines(work / "ssm-delete-all") == ["openci-tf", "engine"]
    assert (work / "operator-cleanup-ran").exists()
    assert "uninstall complete" in completed.stdout


def test_uninstall_stops_at_first_failed_phase(tmp_path):
    """A failed engine-destroy never reaches foundation-destroy or bootstrap-destroy."""
    completed = _run_uninstall_journey(tmp_path, fail_recipe="engine-destroy")
    work = tmp_path / "work"
    assert _lines(work / "just-calls") == ["deploy-destroy", "engine-destroy"]
    assert not (work / "ssm-delete-all").exists()
    assert not (work / "operator-cleanup-ran").exists()
    assert completed.returncode == 3, completed.stderr
    assert "uninstall stopped at failed phase engine-destroy (rc=3)" in completed.stderr
    assert "uninstall complete" not in completed.stdout


def test_uninstall_ssm_cleanup_stops_at_first_failed_namespace(tmp_path):
    completed = _run_uninstall_journey(tmp_path, fail_ssm_project="openci-tf")
    work = tmp_path / "work"
    assert _lines(work / "ssm-delete-all") == ["openci-tf"]
    assert not (work / "operator-cleanup-ran").exists()
    assert completed.returncode == 4, completed.stderr
    assert "uninstall stopped at failed phase ssm-cleanup (rc=4)" in completed.stderr


def test_install_doc_uninstall_states_the_deploy_destroy_limits():
    """deploy-destroy reads the IMAGE_VERSION image and engine/foundation by name."""
    install = (Path(__file__).parents[2] / "docs/INSTALL.md").read_text()
    uninstall = " ".join(install.split("\n## Uninstall\n", 1)[1].split("\n## ", 1)[0].split())
    assert "tagged with the checked-out `IMAGE_VERSION`" in uninstall
    assert "that image must still exist in ECR" in uninstall
    assert "after engine-destroy or a later phase has finished is not proven safe" in uninstall


def _run_recipe_journey(
    tmp_path: Path, recipe: str, stubs: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run one real justfile recipe body with aws, terraform and its scripts stubbed.

    Every stub appends "<name> <args>" to a calls file; the calls come back in order.
    """
    repo_root = Path(__file__).parents[2]
    justfile = (repo_root / "justfile").read_text()
    body_lines = []
    for line in justfile.split(f"\n{recipe}:\n", 1)[1].splitlines():
        if line.strip() and not line.startswith("    "):
            break
        body_lines.append(line[4:])
    body = (
        "\n".join(body_lines)
        .replace("{{OPENCI_TF_PROJECT}}", "openci-tf")
        .replace("{{OPENCI_TF_REGION}}", "us-east-1")
    )
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    for root in ("bootstrap", "foundation", "deploy"):
        (work / "infra" / root).mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(repo_root / "scripts/phase_timing.sh", work / "scripts/phase_timing.sh")
    calls = tmp_path / "calls"
    every_stub = {"aws": 'echo 123456789012\n'} | stubs
    for name, text in every_stub.items():
        path = (work / name) if name.startswith("scripts/") else (bin_dir / name)
        path.write_text(
            f'#!/usr/bin/env bash\necho "{Path(name).name} $*" >> "{calls}"\n{text}'
        )
        path.chmod(0o755)
    completed = subprocess.run(
        ["bash", "-c", body],
        cwd=work,
        env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, calls.read_text().splitlines() if calls.exists() else []


def _bootstrap_destroy_stubs(
    *, terraform: str = "", bucket_owner: str = "echo openci-tf-bootstrap\n"
) -> dict[str, str]:
    return {
        "terraform": terraform,
        "scripts/write_tfvars.sh": "",
        "scripts/bucket_exists.sh": "exit 0\n",
        "scripts/bucket_owner.sh": bucket_owner,
        "scripts/generate_backend.sh": "",
        "scripts/empty_bucket.sh": "",
        "scripts/cleanup_operator_footprint.sh": "",
    }


def test_bootstrap_destroy_stops_when_state_migration_out_of_the_bucket_fails(tmp_path):
    """A failed init -migrate-state never empties the bucket that still holds the state."""
    completed, calls = _run_recipe_journey(
        tmp_path,
        "bootstrap-destroy",
        _bootstrap_destroy_stubs(terraform='case "$*" in *-migrate-state*) exit 6 ;; esac\n'),
    )
    assert completed.returncode == 6, completed.stderr
    assert any("-migrate-state" in call for call in calls)
    assert not any(call.startswith("empty_bucket.sh") for call in calls)
    assert not any(call.startswith("terraform -chdir=infra/bootstrap destroy") for call in calls)
    assert not any(call.startswith("cleanup_operator_footprint.sh") for call in calls)
    assert "<< terraform-destroy FAILED after " in completed.stderr


def test_bootstrap_destroy_aborts_on_unreadable_bucket_tags(tmp_path):
    """bucket_owner.sh exit 2 stops the phase; an empty owner is never compared."""
    completed, calls = _run_recipe_journey(
        tmp_path,
        "bootstrap-destroy",
        _bootstrap_destroy_stubs(bucket_owner="exit 2\n"),
    )
    assert completed.returncode == 2, completed.stderr
    assert "is not owned by openci-tf-bootstrap" not in completed.stderr
    assert not any(call.startswith(("terraform", "empty_bucket.sh")) for call in calls)
    assert "<< terraform-destroy FAILED after " in completed.stderr


def test_deploy_destroy_stops_when_the_stale_lock_guard_fails(tmp_path):
    """A held or unreadable deploy lock stops the phase before terraform destroy."""
    completed, calls = _run_recipe_journey(
        tmp_path,
        "deploy-destroy",
        {
            "terraform": "",
            "scripts/ssm_config.sh": 'echo "$3"\n',
            "scripts/image_tag.sh": "echo 1.13\n",
            "scripts/write_tfvars.sh": "",
            "scripts/generate_backend.sh": "",
            "scripts/terraform_unlock_stale_lock.sh": "exit 1\n",
        },
    )
    assert completed.returncode == 1, completed.stderr
    assert any(call.startswith("terraform_unlock_stale_lock.sh") for call in calls)
    assert not any(call.startswith("terraform -chdir=infra/deploy destroy") for call in calls)
    assert "<< terraform-destroy FAILED after " in completed.stderr


@pytest.mark.parametrize(
    "failing",
    ["scripts/write_tfvars.sh", "scripts/generate_backend.sh", "terraform"],
)
def test_foundation_destroy_stops_at_the_first_failed_step(tmp_path, failing):
    """A failed write_tfvars, generate_backend or init never reaches terraform destroy."""
    stubs = {
        "terraform": "",
        "scripts/ssm_config.sh": 'echo "$3"\n',
        "scripts/write_tfvars.sh": "",
        "scripts/generate_backend.sh": "",
    }
    stubs[failing] = "exit 5\n"
    completed, calls = _run_recipe_journey(tmp_path, "foundation-destroy", stubs)
    assert completed.returncode == 5, completed.stderr
    assert calls[-1].startswith(Path(failing).name)
    assert not any(call.startswith("terraform -chdir=infra/foundation destroy") for call in calls)
    assert "<< terraform-destroy FAILED after " in completed.stderr


@pytest.mark.parametrize("delete_rc", [0, 255])
def test_empty_bucket_batch_file_is_a_mktemp_file_removed_on_exit(tmp_path, delete_rc):
    """The delete batch lives in TMPDIR under a mktemp name, and is gone after any exit."""
    repo_root = Path(__file__).parents[2]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    seen = tmp_path / "delete-arg"
    aws = bin_dir / "aws"
    aws.write_text(
        f"""#!/usr/bin/env bash
case "$*" in
  *head-bucket*) exit 0 ;;
  *list-object-versions*)
    if [ -e "{tmp_path}/listed" ]; then echo '{{"Objects": []}}'; exit 0; fi
    touch "{tmp_path}/listed"
    echo '{{"Objects": [{{"Key": "k", "VersionId": "v"}}]}}' ;;
  *delete-objects*)
    for arg in "$@"; do case "$arg" in file://*) path="${{arg#file://}}" ;; esac; done
    echo "$path" > "{seen}"
    cp "$path" "{tmp_path}/batch-copy"
    exit {delete_rc} ;;
  *) exit 99 ;;
esac
"""
    )
    aws.chmod(0o755)
    completed = subprocess.run(
        ["bash", str(repo_root / "scripts/empty_bucket.sh"), "openci-tf-state-123456789012"],
        cwd=tmp_path,
        env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(tmpdir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == delete_rc, completed.stderr
    batch_path = Path(seen.read_text().strip())
    assert batch_path.parent == tmpdir
    assert json.loads((tmp_path / "batch-copy").read_text()) == {
        "Objects": [{"Key": "k", "VersionId": "v"}],
        "Quiet": True,
    }
    assert list(tmpdir.iterdir()) == []


def test_empty_bucket_stops_on_objects_delete_objects_did_not_delete(tmp_path):
    """DeleteObjects exits 0 and lists refused objects under Errors: name the keys and stop."""
    repo_root = Path(__file__).parents[2]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    deletes = tmp_path / "delete-calls"
    aws = bin_dir / "aws"
    aws.write_text(
        f"""#!/usr/bin/env bash
case "$*" in
  *head-bucket*) exit 0 ;;
  *list-object-versions*)
    echo '{{"Objects": [{{"Key": "kept/terraform.tfstate", "VersionId": "v-kept"}}, {{"Key": "gone", "VersionId": "v-gone"}}]}}' ;;
  *delete-objects*)
    echo "$*" >> "{deletes}"
    [ "$(wc -l < "{deletes}")" -le 3 ] || {{ echo "stub: delete-objects called 4 times" >&2; exit 97; }}
    echo '{{"Errors": [{{"Key": "kept/terraform.tfstate", "VersionId": "v-kept", "Code": "AccessDenied", "Message": "Access Denied"}}]}}' ;;
  *) exit 99 ;;
esac
"""
    )
    aws.chmod(0o755)
    completed = subprocess.run(
        ["bash", str(repo_root / "scripts/empty_bucket.sh"), "openci-tf-state-123456789012"],
        cwd=tmp_path,
        env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 1, completed.stderr
    delete_calls = deletes.read_text().splitlines()
    assert len(delete_calls) == 1
    assert "--output json" in delete_calls[0]
    assert (
        "ERROR: S3 did not delete 1 of 2 versions from openci-tf-state-123456789012 (AccessDenied); keys:"
        in completed.stderr
    )
    assert "\nkept/terraform.tfstate\n" in completed.stderr
    assert "v-kept" not in completed.stderr + completed.stdout
    assert "deleted 2 versions" not in completed.stdout
    assert "emptied" not in completed.stdout


def test_justfile_worktree_link_comment_makes_every_import_resolve(tmp_path):
    """The header's ln line, run in a sibling worktree, links the gitignored kit modules."""
    justfile = (Path(__file__).parents[2] / "justfile").read_text()
    header = justfile.split("\nset positional-arguments", 1)[0].splitlines()
    ln_line = next(line.lstrip("# ") for line in header if line.lstrip("# ").startswith("ln "))
    imports = [line.split("'")[1] for line in header if line.startswith("import '")]
    assert imports
    main = tmp_path / "openci-tf"
    worktree = tmp_path / "openci-tf-wt-x"
    for path in imports:
        (main / path).parent.mkdir(parents=True, exist_ok=True)
        (main / path).write_text("")
    # .shared-llm/this_repo is tracked, so every worktree already has .shared-llm/.
    (worktree / ".shared-llm/this_repo").mkdir(parents=True)
    subprocess.run(["bash", "-c", ln_line], cwd=worktree, check=True)
    for path in imports:
        assert (worktree / path).is_file(), f"{ln_line} does not make {path} resolve"


def test_deploy_destroy_unlocks_stale_terraform_lock_before_destroy():
    justfile = (Path(__file__).parents[2] / "justfile").read_text()
    section = justfile.split("deploy-destroy:", 1)[1].split("target-create-aws-readonly", 1)[0]
    unlock_index = section.index("./scripts/terraform_unlock_stale_lock.sh")
    destroy_index = section.index("terraform -chdir=infra/deploy destroy")
    assert unlock_index < destroy_index


def test_verify_clean_checks_operator_ssm_log_groups_and_legacy_roles():
    script = (Path(__file__).parents[2] / "scripts/verify.sh").read_text()
    assert "product_log_groups.sh" in script
    assert "ssm_prefix_params_exist" in script
    for prefix in (
        "/openci-tf/clone-token",
        "/openci-tf/env",
        "/openci-tf/infracost",
        "/openci-tf/webhook",
    ):
        assert prefix in script
    assert 'check "log group ${log_group}"' in script
    assert "product_log_group_names" in script
    assert 'check_role "legacy role ${PROJECT}-${r}"' in script


def test_terraform_unlock_stale_lock_reports_lock_with_force_unlock_command(
    tmp_path: Path,
):
    repo_root = Path(__file__).parents[2]
    script = repo_root / "scripts/terraform_unlock_stale_lock.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    unlock_log = tmp_path / "unlock.log"

    (bin_dir / "aws").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"s3api get-object"*)
    cat <<'JSON'
{"ID":"c5bcf01c-1ebf-b1e3-9f3a-55a995eef2cc","Created":"2020-01-01T00:00:00Z","Who":"test@host","Operation":"OperationTypeApply"}
JSON
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 99
    ;;
esac
"""
    )
    (bin_dir / "terraform").write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"{unlock_log}"
"""
    )
    (bin_dir / "aws").chmod(0o755)
    (bin_dir / "terraform").chmod(0o755)

    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "AWS_REGION": "us-east-1"}
    completed = subprocess.run(
        [
            "bash",
            str(script),
            "infra/deploy",
            "openci-tf-state-123456789012",
            "deploy",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stdout
    assert "terraform -chdir=infra/deploy force-unlock c5bcf01c-1ebf-b1e3-9f3a-55a995eef2cc" in completed.stderr
    assert "test@host" in completed.stderr
    assert not unlock_log.exists()


def test_terraform_unlock_stale_lock_exits_zero_when_no_lock(tmp_path: Path):
    repo_root = Path(__file__).parents[2]
    script = repo_root / "scripts/terraform_unlock_stale_lock.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    (bin_dir / "aws").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"s3api get-object"*)
    echo "An error occurred (NoSuchKey) when calling the GetObject operation" >&2
    exit 254
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 99
    ;;
esac
"""
    )
    (bin_dir / "aws").chmod(0o755)

    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "AWS_REGION": "us-east-1"}
    completed = subprocess.run(
        [
            "bash",
            str(script),
            "infra/deploy",
            "openci-tf-state-123456789012",
            "deploy",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_terraform_unlock_stale_lock_fails_with_exact_command_for_fresh_lock(
    tmp_path: Path,
):
    repo_root = Path(__file__).parents[2]
    script = repo_root / "scripts/terraform_unlock_stale_lock.sh"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    (bin_dir / "aws").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"s3api get-object"*)
    cat <<'JSON'
{"ID":"fresh-lock-id","Created":"2099-01-01T00:00:00Z","Who":"test@host","Operation":"OperationTypeApply"}
JSON
    ;;
  *)
    echo "unexpected aws call: $*" >&2
    exit 99
    ;;
esac
"""
    )
    (bin_dir / "terraform").write_text("#!/usr/bin/env bash\nexit 99\n")
    (bin_dir / "aws").chmod(0o755)
    (bin_dir / "terraform").chmod(0o755)

    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "AWS_REGION": "us-east-1"}
    completed = subprocess.run(
        [
            "bash",
            str(script),
            "infra/deploy",
            "openci-tf-state-123456789012",
            "deploy",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "terraform -chdir=infra/deploy force-unlock fresh-lock-id" in completed.stderr


def test_presign_put_sets_content_type_for_text_artifacts(monkeypatch):
    captured = {}

    class Client:
        def generate_presigned_url(self, method, *, Params, ExpiresIn):
            captured.update(
                {"method": method, "Params": Params, "ExpiresIn": ExpiresIn}
            )
            return "https://signed"

    monkeypatch.setattr(s3, "_presign_client", lambda: Client())
    s3.presign_put("tmp", "openci-tf/org/repo/run/infra/a/init.out", 900)
    assert captured["Params"]["ContentType"] == "text/plain"
