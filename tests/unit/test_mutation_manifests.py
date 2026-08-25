# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behavioral manifest build/collect coverage for apply/destroy actions."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.engine.manifest import (
    BucketSet,
    ManifestBinding,
    _canonical_manifest_digest,
    build_failure_manifest,
    build_manifest,
    validate_manifest_schema,
)
from src.services.run_folder import collect


def _head_objects(fixtures: dict[str, bytes]):
    def head_object(bucket: str, key: str):
        body = fixtures.get(f"{bucket}/{key}")
        if body is None:
            return None
        content_type = "binary/octet-stream"
        if key.endswith(".zip"):
            content_type = "application/octet-stream"
        elif not key.endswith("/done"):
            content_type = "text/plain"
        return {
            "content_length": len(body),
            "content_type": content_type,
            "last_modified": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }

    return head_object


def _read_objects(fixtures: dict[str, bytes]):
    def read_object_bytes(bucket: str, key: str, max_bytes: int):
        body = fixtures.get(f"{bucket}/{key}")
        if body is None or len(body) > max_bytes:
            return None
        return body

    return read_object_bytes


@pytest.mark.parametrize("action,output_name", [("apply", "apply.out"), ("destroy", "destroy.out")])
def test_mutation_manifest_build_and_schema(action, output_name):
    repo = "org/repo"
    run_id = "run-apply"
    folder = "infra/vpc"
    exec_id = f"{run_id}.{folder}.0"
    prefix = f"openci-tf/{repo}/{run_id}/{folder}/"
    fixtures = {
        f"tmp/{prefix}init.out": b"init\n",
        f"tmp/{prefix}validate.out": b"ok\n",
        f"tmp/{prefix}plan-show.out": b"plan show\n",
        f"tmp/{prefix}{output_name}": b"applied\n",
        f"done/{exec_id}/done": b"{}",
        f"pkg/{exec_id}.zip": b"zip",
    }
    manifest = build_manifest(
        execution_id=exec_id,
        buckets=BucketSet(
            tmp_bucket="tmp",
            done_bucket="done",
            package_bucket="pkg",
            done_uri=f"s3://done/{exec_id}/done",
            package_uri=f"s3://pkg/{exec_id}.zip",
        ),
        binding=ManifestBinding(
            run_id=run_id,
            repo_name=repo,
            commit_hash="a" * 40,
            account_id="123456789012",
            folder=folder,
            attempt=0,
            source_plan_run_id="source-plan-run",
        ),
        action=action,
        head_object=_head_objects(fixtures),
        read_object_bytes=_read_objects(fixtures),
        plan_metadata=None,
    )
    validate_manifest_schema(manifest, execution_id=exec_id)
    assert manifest["source_plan_run_id"] == "source-plan-run"


def test_plan_destroy_failure_manifest_schema():
    manifest = build_failure_manifest(
        execution_id="run.infra.0",
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action="plan_destroy",
        failure_reason="plan failed",
        run_id="run",
        repo_name="org/repo",
        commit_hash="a" * 40,
        account_id="123456789012",
        folder="infra",
        attempt=0,
        generated_at_source=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    validate_manifest_schema(manifest, execution_id="run.infra.0")


def test_collect_passes_source_plan_run_id(monkeypatch):
    captured: dict = {}

    def fake_build_manifest(**kwargs):
        captured.update(kwargs)
        binding = kwargs["binding"]
        captured["source_plan_run_id"] = binding.source_plan_run_id
        return {
            "manifest_s3_uri": "s3://tmp/key/manifest.json",
            "manifest_sha256": "a" * 64,
            "entries": [],
            "version": 1,
            "execution_id": kwargs["execution_id"],
            "action": kwargs["action"],
            "generated_at": "2026-01-01T00:00:00Z",
            "package_bucket": "pkg",
            "tmp_bucket": "tmp",
            "done_bucket": "done",
            "plan_retention_days": 1,
            "run_id": binding.run_id,
            "repo_name": binding.repo_name,
            "commit_hash": binding.commit_hash,
            "account_id": binding.account_id,
            "folder": binding.folder,
            "attempt": binding.attempt,
            "source_plan_run_id": binding.source_plan_run_id,
        }

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setattr(collect, "build_manifest", fake_build_manifest)
    monkeypatch.setattr(collect, "put_json_create_only", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collect, "put_folder_attempt", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "validate_outer_child_output", lambda *_args, **_kwargs: None)
    event = {
        "exec_id": "run.infra.0",
        "attempt": 0,
        "succeeded": True,
        "credential_expired": False,
        "steps": [],
        "error": None,
        "pointers": {"done": "s3://done/run.infra.0/done"},
        "action": "apply",
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "account_id": "123456789012",
        "folder": "infra",
        "run_id": "run",
        "submitted_at": 1_700_000_000.0,
        "source_plan_run_id": "source-plan",
    }
    collect.handler(event, object())
    assert captured["source_plan_run_id"] == "source-plan"


def _manifest_entry(name: str) -> dict[str, object]:
    return {
        "name": name,
        "s3_uri": f"s3://tmp/openci-tf/org/repo/run/infra/{name}",
        "content_type": "text/plain",
        "size": 1,
        "checksum": "a" * 64,
        "expires_at": "2099-01-01T00:00:00Z",
    }


@pytest.mark.parametrize("action,output_name", [("apply", "apply.out"), ("destroy", "destroy.out")])
def test_failed_mutation_manifest_with_plan_show_validates(action, output_name):
    manifest = build_failure_manifest(
        execution_id="run.infra.0",
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action=action,
        failure_reason="saved plan is stale",
        run_id="run",
        repo_name="org/repo",
        commit_hash="a" * 40,
        account_id="123456789012",
        folder="infra",
        attempt=0,
        generated_at_source=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_plan_run_id="plan-run",
    )
    manifest["entries"] = [
        _manifest_entry("init.out"),
        _manifest_entry("validate.out"),
        _manifest_entry("plan-show.out"),
        _manifest_entry(output_name),
    ]
    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)
    validate_manifest_schema(manifest, execution_id="run.infra.0")


@pytest.mark.parametrize("action", ["apply", "destroy"])
def test_failed_mutation_manifest_rejects_unrelated_extra_entry(action):
    manifest = build_failure_manifest(
        execution_id="run.infra.0",
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action=action,
        failure_reason="engine failed",
        run_id="run",
        repo_name="org/repo",
        commit_hash="a" * 40,
        account_id="123456789012",
        folder="infra",
        attempt=0,
        generated_at_source=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_plan_run_id="plan-run",
    )
    manifest["entries"] = [
        _manifest_entry("init.out"),
        _manifest_entry("validate.out"),
        _manifest_entry("plan-show.out"),
        _manifest_entry("apply.out" if action == "apply" else "destroy.out"),
        _manifest_entry("tf/plan.out"),
    ]
    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)
    with pytest.raises(ValueError, match="unexpected entries"):
        validate_manifest_schema(manifest, execution_id="run.infra.0")
