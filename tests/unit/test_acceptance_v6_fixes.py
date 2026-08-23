# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production-shaped tests for acceptance-v6 blockers C1-C4."""
from __future__ import annotations

import copy
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.domain.engine.artifact_limits import (
    MAX_MANIFEST_BYTES,
)
from src.domain.engine.manifest import (
    BucketSet,
    ManifestBinding,
    _canonical_manifest_digest,
    build_failure_manifest,
    build_manifest,
    validate_manifest_schema,
)
from src.domain.engine.plan_artifacts import (
    plan_retention_days,
    validate_plan_artifact_metadata,
)
from src.domain.engine.artifact_paths import manifest_key
from src.platform.aws import s3
from src.services.run_folder import collect, prepare_and_submit, write_failure_manifest
from tests.unit.manifest_fixtures import (
    committed_success_plan_manifest,
    complete_plan_object_mocks,
    plan_metadata_dict,
)


def _digest_manifest(manifest: dict) -> dict:
    updated = copy.deepcopy(manifest)
    updated["manifest_sha256"] = _canonical_manifest_digest(updated)
    return updated


def test_package_upload_sets_application_zip_content_type(monkeypatch):
    captured: dict = {}

    class Client:
        def upload_file(self, path, bucket, key, ExtraArgs=None):
            captured.update({"path": path, "bucket": bucket, "key": key, "ExtraArgs": ExtraArgs or {}})

    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: Client())
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        with zipfile.ZipFile(tmp.name, "w") as archive:
            archive.writestr("payload.txt", "x")
        s3.upload_file(tmp.name, "pkg", "run.zip", content_type="application/zip")
    assert captured["ExtraArgs"] == {"ContentType": "application/zip"}


def test_prepare_bounded_package_upload_uses_application_zip(monkeypatch):
    captured: dict = {}

    def fake_upload(path, bucket, key, *, content_type=None):
        captured.update({"path": path, "bucket": bucket, "key": key, "content_type": content_type})

    monkeypatch.setattr(prepare_and_submit.s3, "upload_file", fake_upload)
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        tmp.write(b"PK\x03\x04")
        tmp.flush()
        prepare_and_submit._upload_bounded_package(tmp.name, "pkg", "run.zip")
    assert captured["content_type"] == "application/zip"


def test_done_marker_records_binary_octet_stream_from_head_object(monkeypatch):
    last_modified = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id="run.abc.0",
        repo_name="org/repo",
        run_id="run",
        commit_hash="b" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        last_modified=last_modified,
    )

    def head_with_engine_done(bucket: str, key: str):
        meta = head_object(bucket, key)
        if key.endswith("/done"):
            assert meta is not None
            meta = dict(meta)
            meta["content_type"] = "binary/octet-stream"
        return meta

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
            run_id="run",
            repo_name="org/repo",
            commit_hash="b" * 40,
            account_id="123456789012",
            folder="infra/a",
            attempt=0,
        ),
        action="plan",
        head_object=head_with_engine_done,
        read_object_bytes=read_object_bytes,
        plan_metadata=plan_metadata,
        plan_dimensions={
            "repo_name": "org/repo",
            "commit_hash": "b" * 40,
            "account_id": "123456789012",
            "folder": "infra/a",
            "attempt": 0,
        },
        generated_at_source=last_modified,
    )
    done_entry = next(entry for entry in manifest["entries"] if entry["name"] == "done")
    assert done_entry["content_type"] == "binary/octet-stream"
    package_entry = next(entry for entry in manifest["entries"] if entry["name"] == "package")
    assert package_entry["content_type"] == "application/zip"


def test_render_pr_lambda_receives_plan_retention_days():
    lambdas = (Path(__file__).parents[2] / "infra/deploy/modules/openci_tf/lambdas.tf").read_text()
    assert 'contains(["api", "render-pr", "intent-create"], each.key)' in lambdas
    assert "PLAN_RETENTION_DAYS" in lambdas


def test_terraform_lifecycle_variables_require_integral_values():
    for rel in (
        "infra/foundation/variables.tf",
        "infra/deploy/variables.tf",
        "infra/deploy/modules/run_folder/variables.tf",
        "infra/deploy/modules/openci_tf/variables.tf",
    ):
        text = (Path(__file__).parents[2] / rel).read_text()
        assert "floor(var.tmp_lifecycle_days) == var.tmp_lifecycle_days" in text
        assert "floor(var.plan_retention_days) == var.plan_retention_days" in text


def test_non_default_plan_retention_reaches_renderer(monkeypatch):
    monkeypatch.setenv("PLAN_RETENTION_DAYS", "7")
    metadata = plan_metadata_dict(
        bucket="tmp",
        repo_name="org/repo",
        run_id="run",
        commit_hash="b" * 40,
        account_id="123456789012",
        folder="infra/a",
        retention_days=7,
    )
    metadata["expires_at"] = "2026-08-17T00:00:00Z"
    validate_plan_artifact_metadata(
        metadata=metadata,
        bucket="tmp",
        repo_name="org/repo",
        run_id="run",
        commit_hash="b" * 40,
        account_id="123456789012",
        folder="infra/a",
        action="plan",
    )
    assert plan_retention_days() == 7


@pytest.mark.parametrize(
    ("mutator", "pattern"),
    [
        (
            lambda manifest: next(entry for entry in manifest["entries"] if entry["name"] == "plan-metadata.json").__setitem__("size", 4097),
            "size bound",
        ),
        (
            lambda manifest: next(entry for entry in manifest["entries"] if entry["name"] == "plan-metadata.json").__setitem__("size", True),
            "non-negative integer",
        ),
        (lambda manifest: manifest.__setitem__("attempt", True), "manifest attempt must be a non-negative integer"),
        (lambda manifest: manifest.__setitem__("plan_retention_days", 2.5), "non-negative integer"),
        (
            lambda manifest: next(entry for entry in manifest["entries"] if entry["name"] == "done").__setitem__("content_type", "application/json"),
            "binary/octet-stream",
        ),
    ],
)
def test_manifest_rejects_digest_correct_malformed_probes(mutator, pattern: str):
    manifest = committed_success_plan_manifest(
        execution_id="run.abc.0",
        commit_hash="c" * 40,
    )
    mutated = _digest_manifest(manifest)
    mutator(mutated)
    with pytest.raises(ValueError, match=pattern):
        validate_manifest_schema(mutated, execution_id="run.abc.0")


def test_manifest_rejects_boolean_entry_size():
    manifest = committed_success_plan_manifest(execution_id="run.abc.0", commit_hash="c" * 40)
    mutated = copy.deepcopy(manifest)
    mutated["entries"][0]["size"] = True
    mutated = _digest_manifest(mutated)
    with pytest.raises(ValueError, match="non-negative integer"):
        validate_manifest_schema(mutated, execution_id="run.abc.0")


@pytest.mark.parametrize(
    ("bucket_field", "pattern"),
    [
        ("done_bucket", "done_bucket mismatch"),
        ("package_bucket", "package_bucket mismatch"),
        ("tmp_bucket", "manifest_s3_uri does not match expected execution topology"),
    ],
)
def test_collect_rejects_wrong_existing_physical_bucket(bucket_field: str, pattern: str, monkeypatch):
    exec_id = "run.0123456789ab.0"
    committed = committed_success_plan_manifest(
        execution_id=exec_id,
        commit_hash="c" * 40,
    )
    wrong = f"other-{bucket_field.removesuffix('_bucket')}"
    committed[bucket_field] = wrong
    if bucket_field == "done_bucket":
        for entry in committed["entries"]:
            if entry["name"] == "done":
                entry["s3_uri"] = f"s3://{wrong}/{exec_id}/done"
    elif bucket_field == "package_bucket":
        for entry in committed["entries"]:
            if entry["name"] == "package":
                entry["s3_uri"] = f"s3://{wrong}/{exec_id}.zip"
    committed = _digest_manifest(committed)
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
    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")))
    monkeypatch.setattr(collect, "get_bounded_json", lambda bucket, key, limit: committed if key.endswith("manifest.json") else plan_metadata)
    with pytest.raises(ValueError, match=pattern):
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


def test_collect_rejects_oversized_existing_manifest(monkeypatch):
    exec_id = "run.0123456789ab.0"
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

    def oversized_manifest(*_args, **_kwargs):
        raise ValueError(f"object exceeds {MAX_MANIFEST_BYTES} bytes")

    monkeypatch.setattr(collect, "get_bounded_json", oversized_manifest)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")))
    with pytest.raises(ValueError, match=str(MAX_MANIFEST_BYTES)):
        collect.handler(
            {
                "exec_id": exec_id,
                "attempt": 0,
                "succeeded": True,
                "credential_expired": False,
                "steps": [],
                "error": None,
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


def test_put_json_create_only_uses_bounded_manifest_read(monkeypatch):
    client = Mock()
    client.put_object.side_effect = __import__("botocore.exceptions", fromlist=["ClientError"]).ClientError(
        {"Error": {"Code": "PreconditionFailed", "Message": "exists"}},
        "PutObject",
    )
    client.head_object.return_value = {"ContentLength": MAX_MANIFEST_BYTES + 1}
    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: client)
    with pytest.raises(ValueError, match=str(MAX_MANIFEST_BYTES)):
        s3.put_json_create_only("tmp", "run/manifest.json", {"version": 1})


@pytest.mark.parametrize(
    ("bucket_field", "pattern"),
    [
        ("done_bucket", "done_bucket mismatch"),
        ("package_bucket", "package_bucket mismatch"),
        ("tmp_bucket", "manifest_s3_uri does not match expected execution topology"),
    ],
)
def test_failure_writer_rejects_copied_declared_digest_wrong_bucket(
    bucket_field: str, pattern: str, monkeypatch
):
    """C1: copied digest must not bypass schema/binding/current-bucket validation."""
    exec_id = "run.0123456789ab.0"
    attempted = build_failure_manifest(
        execution_id=exec_id,
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action="plan",
        failure_reason="late failure",
        run_id="run",
        repo_name="org/repo",
        commit_hash="c" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        generated_at_source=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )
    existing = copy.deepcopy(committed_success_plan_manifest(execution_id=exec_id, commit_hash="c" * 40))
    wrong = f"other-{bucket_field.removesuffix('_bucket')}"
    existing[bucket_field] = wrong
    if bucket_field == "done_bucket":
        for entry in existing["entries"]:
            if entry["name"] == "done":
                entry["s3_uri"] = f"s3://{wrong}/{exec_id}/done"
    elif bucket_field == "package_bucket":
        for entry in existing["entries"]:
            if entry["name"] == "package":
                entry["s3_uri"] = f"s3://{wrong}/{exec_id}.zip"
    existing = _digest_manifest(existing)
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    monkeypatch.setattr(write_failure_manifest, "get_bounded_json", lambda *_args, **_kwargs: existing)
    with pytest.raises(ValueError, match=pattern):
        write_failure_manifest._persist_manifest(
            "tmp",
            "done",
            "pkg",
            manifest_key("org/repo", "run", "infra/a"),
            exec_id,
            attempted,
        )


def test_failure_writer_rejects_copied_declared_digest_malformed_body(monkeypatch):
    """Copied digest field from attempted manifest must not bypass canonical validation."""
    exec_id = "run.0123456789ab.0"
    attempted = build_failure_manifest(
        execution_id=exec_id,
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action="plan",
        failure_reason="late failure",
        run_id="run",
        repo_name="org/repo",
        commit_hash="c" * 40,
        account_id="123456789012",
        folder="infra/a",
        attempt=0,
        generated_at_source=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )
    existing = copy.deepcopy(committed_success_plan_manifest(execution_id=exec_id, commit_hash="c" * 40))
    existing["done_bucket"] = "other-done"
    for entry in existing["entries"]:
        if entry["name"] == "done":
            entry["s3_uri"] = f"s3://other-done/{exec_id}/done"
    existing["manifest_sha256"] = attempted["manifest_sha256"]
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("exists")),
    )
    monkeypatch.setattr(write_failure_manifest, "get_bounded_json", lambda *_args, **_kwargs: existing)
    with pytest.raises(ValueError, match="digest mismatch"):
        write_failure_manifest._persist_manifest(
            "tmp",
            "done",
            "pkg",
            manifest_key("org/repo", "run", "infra/a"),
            exec_id,
            attempted,
        )


@pytest.mark.parametrize(
    ("entry_name", "zero_size"),
    [
        ("package", 0),
        ("done", 0),
        ("plan.tfplan", 0),
        ("plan.tfplan.sha256", 0),
        ("plan-metadata.json", 0),
    ],
)
def test_manifest_rejects_zero_size_required_non_empty_entries(entry_name: str, zero_size: int):
    manifest = committed_success_plan_manifest(execution_id="run.abc.0", commit_hash="c" * 40)
    mutated = copy.deepcopy(manifest)
    entry = next(item for item in mutated["entries"] if item["name"] == entry_name)
    entry["size"] = zero_size
    mutated = _digest_manifest(mutated)
    with pytest.raises(ValueError, match="below minimum size"):
        validate_manifest_schema(mutated, execution_id="run.abc.0")


@pytest.mark.parametrize("action", ["plan", "drift", "report"])
def test_build_manifest_rejects_negative_attempt(action: str):
    last_modified = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="non-negative integer"):
        build_manifest(
            execution_id="run.abc.-1",
            buckets=BucketSet(
                tmp_bucket="tmp",
                done_bucket="done",
                package_bucket="pkg",
                done_uri="s3://done/run.abc.-1/done",
                package_uri="s3://pkg/run.abc.-1.zip",
            ),
            binding=ManifestBinding(attempt=-1),
            action=action,
            head_object=lambda *_args, **_kwargs: None,
            read_object_bytes=lambda *_args, **_kwargs: None,
            plan_metadata=None,
            plan_dimensions=None,
            generated_at_source=last_modified,
        )


def test_build_failure_manifest_rejects_negative_attempt():
    with pytest.raises(ValueError, match="non-negative integer"):
        build_failure_manifest(
            execution_id="run.abc.-1",
            tmp_bucket="tmp",
            done_bucket="done",
            package_bucket="pkg",
            action="drift",
            failure_reason="failed",
            run_id="run",
            repo_name="org/repo",
            commit_hash="c" * 40,
            account_id="123456789012",
            folder="infra/a",
            attempt=-1,
            generated_at_source=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
        )


def test_manifest_rejects_negative_attempt_in_schema():
    manifest = committed_success_plan_manifest(execution_id="run.abc.0", commit_hash="c" * 40)
    mutated = copy.deepcopy(manifest)
    mutated["attempt"] = -1
    mutated = _digest_manifest(mutated)
    with pytest.raises(ValueError, match="non-negative integer"):
        validate_manifest_schema(mutated, execution_id="run.abc.0")
