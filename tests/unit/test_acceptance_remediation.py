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
        lambda *_args, **_kwargs: None,
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
        "account_alias: target\ntf_runtime: tofu:1.8.0\n"
    )
    paths = discover_folder_paths(root)
    assert list(paths.keys()) == ["é"]
    assert paths["é"] == "e\u0301"
    resolved = resolve_outer_state(
        str(root), ["é"], {"tofu:1.8.0": "https://example.com/tofu"}, "drift"
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
    assert 'CANONICAL_STATE_KEY="engine/terraform.tfstate"' in script
    assert 'LEGACY_STATE_KEY="engine-02-deploy/terraform.tfstate"' in script
    assert 'generate_backend.sh" "$STATE_BUCKET" engine-ecr' in script
    assert "mirror-image.sh" in script
    assert 'rev-parse --short=7 HEAD' in script
    assert "engine_image_uri" in script
    assert 'tofu -chdir="$ECR_DIR" output -raw repository_url' in script
    assert "generate_tfvars.sh" in script
    assert "init -migrate-state -force-copy -input=false" in script
    assert "state list)" in script
    assert 'delete-object --bucket "$STATE_BUCKET" --key "$LEGACY_STATE_KEY"' in script
    assert 'delete_checksum_row "$CANONICAL_STATE_KEY"' in script
    assert 'delete_checksum_row "$LEGACY_STATE_KEY"' in script
    assert "attribute_not_exists(Info)" in script
    assert "both canonical and legacy engine state objects exist" in script
    assert (
        'upload_source.sh" "$STATE_BUCKET" engine "$ENGINE_ROOT" infra/02-deploy'
        in script
    )


def test_engine_uninstall_script_reverses_ecr_then_deploy():
    script = (Path(__file__).parents[2] / "scripts/engine_uninstall.sh").read_text()
    deploy_index = script.index('generate_backend.sh" "$STATE_BUCKET" engine')
    ecr_index = script.index('generate_backend.sh" "$STATE_BUCKET" engine-ecr')
    assert deploy_index < ecr_index
    assert "infra/02-deploy" in script
    assert "infra/01-ecr" in script
    assert "tofu destroy -input=false -auto-approve" in script
    assert "batch-delete-image" not in script


def test_verify_uses_canonical_engine_source_and_bucket_names():
    script = (Path(__file__).parents[2] / "scripts/verify.sh").read_text()
    assert "bootstrap foundation deploy engine" in script
    assert "target-connect/terraform.tfstate" in script
    assert "source copy target-connect" in script
    assert "bootstrap foundation deploy target-connect engine" not in script
    assert "bootstrap foundation deploy target-connect engine-02-deploy" not in script
    assert 'for b in internal "done"; do' in script
    assert "engine bucket ${PROJECT}-${b}-${ACCOUNT_ID}" not in script


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
