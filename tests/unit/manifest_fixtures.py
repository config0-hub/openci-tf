# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for complete successful plan/report manifest tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from src.domain.engine.artifact_limits import (
    MAX_BINARY_PLAN_BYTES,
    MAX_CHECKSUM_SIDECAR_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PLAN_METADATA_BYTES,
    MAX_RAW_ARTIFACT_BYTES,
)
from src.domain.engine.artifact_paths import (
    build_folder_artifact_keys,
    expected_plan_artifact_uris,
    manifest_key,
)
from src.domain.engine.plan_artifacts import plan_retention_days


def plan_metadata_dict(
    *,
    bucket: str,
    repo_name: str,
    run_id: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    action: str = "plan",
    plan_body: bytes = b"x",
    retention_days: int | None = None,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> dict[str, Any]:
    configured_retention = plan_retention_days() if retention_days is None else retention_days
    expected = expected_plan_artifact_uris(
        bucket=bucket,
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    digest = hashlib.sha256(plan_body).hexdigest()
    return {
        "repo": repo_name,
        "run_id": run_id,
        "pinned_sha": commit_hash.lower(),
        "account_id": account_id,
        "folder": folder,
        "action": action,
        "opentofu_runtime": "tofu:1.8.0",
        "created_at": "2026-08-10T00:00:00Z",
        "expires_at": "2026-08-11T00:00:00Z",
        "expires_after_days": configured_retention,
        "plan_s3_uri": expected.plan,
        "sha256_s3_uri": expected.checksum,
        "metadata_s3_uri": expected.metadata,
        "sha256": digest,
    }


def committed_success_plan_manifest(
    *,
    execution_id: str,
    tmp_bucket: str = "tmp",
    done_bucket: str = "done",
    package_bucket: str = "pkg",
    repo_name: str = "org/repo",
    run_id: str = "run",
    commit_hash: str,
    account_id: str = "123456789012",
    folder: str = "infra/a",
    attempt: int = 0,
    action: str = "plan",
) -> dict[str, Any]:
    """Minimal schema-valid committed success manifest for reconciliation tests."""
    expires = "2026-08-12T00:00:00Z"
    checksum = "a" * 64
    keys = build_folder_artifact_keys(repo_name=repo_name, run_id=run_id, folder_path=folder)
    expected = expected_plan_artifact_uris(
        bucket=tmp_bucket,
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
    )
    entries = [
        {"name": "init.out", "s3_uri": f"s3://{tmp_bucket}/{keys.init_out}", "content_type": "text/plain", "size": 1, "checksum": checksum, "expires_at": expires},
        {"name": "validate.out", "s3_uri": f"s3://{tmp_bucket}/{keys.validate_out}", "content_type": "text/plain", "size": 1, "checksum": checksum, "expires_at": expires},
        {"name": "tf/plan.out", "s3_uri": f"s3://{tmp_bucket}/{keys.plan_out}", "content_type": "text/plain", "size": 1, "checksum": checksum, "expires_at": expires},
        {"name": "tfsec.json", "s3_uri": f"s3://{tmp_bucket}/{keys.tfsec_json}", "content_type": "application/json", "size": 2, "checksum": checksum, "expires_at": expires},
        {"name": "tfsec.output", "s3_uri": f"s3://{tmp_bucket}/{keys.tfsec_output}", "content_type": "text/plain", "size": 1, "checksum": checksum, "expires_at": expires},
        {"name": "infracost.json", "s3_uri": f"s3://{tmp_bucket}/{keys.infracost_json}", "content_type": "application/json", "size": 2, "checksum": checksum, "expires_at": expires},
        {"name": "done", "s3_uri": f"s3://{done_bucket}/{execution_id}/done", "content_type": "binary/octet-stream", "size": 1, "checksum": checksum, "expires_at": expires},
        {"name": "package", "s3_uri": f"s3://{package_bucket}/{execution_id}.zip", "content_type": "application/zip", "size": 1, "checksum": checksum, "expires_at": expires},
        {"name": "plan.tfplan", "s3_uri": expected.plan, "content_type": "application/octet-stream", "size": 1, "checksum": checksum, "expires_at": expires},
        {"name": "plan.tfplan.sha256", "s3_uri": expected.checksum, "content_type": "text/plain", "size": 64, "checksum": checksum, "expires_at": expires},
        {"name": "plan-metadata.json", "s3_uri": expected.metadata, "content_type": "application/json", "size": 2, "checksum": checksum, "expires_at": expires},
    ]
    manifest: dict[str, Any] = {
        "version": 1,
        "execution_id": execution_id,
        "action": action,
        "generated_at": "2026-08-10T12:00:00Z",
        "manifest_s3_uri": f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}",
        "entries": entries,
        "package_bucket": package_bucket,
        "tmp_bucket": tmp_bucket,
        "done_bucket": done_bucket,
        "plan_retention_days": 1,
        "run_id": run_id,
        "repo_name": repo_name,
        "commit_hash": commit_hash,
        "account_id": account_id,
        "folder": folder,
        "attempt": attempt,
    }
    from src.domain.engine.manifest import _canonical_manifest_digest

    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)
    return manifest


def complete_plan_object_mocks(
    *,
    execution_id: str,
    repo_name: str,
    run_id: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    attempt: int,
    last_modified: datetime,
    plan_body: bytes = b"x",
    package_body: bytes | None = None,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> tuple[dict[str, Any], Callable[..., dict[str, Any] | None], Callable[..., bytes | None]]:
    metadata = plan_metadata_dict(
        bucket="tmp",
        repo_name=repo_name,
        run_id=run_id,
        commit_hash=commit_hash,
        account_id=account_id,
        folder=folder,
        plan_body=plan_body,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    plan_digest = metadata["sha256"]
    metadata_body = json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode()
    if package_body is None:
        package_body = plan_body

    def head_object(_bucket: str, key: str) -> dict[str, Any] | None:
        if key.endswith(".zip"):
            return {
                "content_length": len(package_body),
                "content_type": "application/zip",
                "last_modified": last_modified,
                "checksum_sha256": hashlib.sha256(package_body).hexdigest(),
            }
        if key.endswith(("init.out", "validate.out", "plan.out")):
            content_type = "text/plain"
        elif key.endswith(("/done", "done")):
            content_type = "binary/octet-stream"
        elif key.endswith("plan.tfplan"):
            content_type = "application/octet-stream"
        elif key.endswith("plan.tfplan.sha256"):
            content_type = "text/plain"
        elif key.endswith(".json"):
            content_type = "application/json"
        else:
            content_type = "text/plain"
        if key.endswith(".zip"):
            size = len(package_body)
            checksum = hashlib.sha256(package_body).hexdigest()
        elif key.endswith("plan.tfplan"):
            size = len(plan_body)
            checksum = plan_digest
        elif key.endswith("plan.tfplan.sha256"):
            size = len(f"{plan_digest}\n")
            checksum = hashlib.sha256(f"{plan_digest}\n".encode()).hexdigest()
        elif key.endswith(".json"):
            if key.endswith("plan-metadata.json"):
                size = len(metadata_body)
                checksum = hashlib.sha256(metadata_body).hexdigest()
            else:
                size = 2
                checksum = "a" * 64
        else:
            size = 1
            checksum = "a" * 64
        return {
            "content_length": size,
            "content_type": content_type,
            "last_modified": last_modified,
            "checksum_sha256": checksum,
        }

    def read_object_bytes(_bucket: str, key: str, max_bytes: int) -> bytes | None:
        if key.endswith("plan.tfplan"):
            body = plan_body
            limit = MAX_BINARY_PLAN_BYTES
        elif key.endswith("plan.tfplan.sha256"):
            body = f"{plan_digest}\n".encode()
            limit = MAX_CHECKSUM_SIDECAR_BYTES
        elif key.endswith("plan-metadata.json"):
            body = metadata_body
            limit = MAX_PLAN_METADATA_BYTES
        elif key.endswith(".zip"):
            body = package_body
            limit = MAX_PACKAGE_BYTES
        else:
            body = b"x"
            limit = MAX_RAW_ARTIFACT_BYTES
        if len(body) > max_bytes or len(body) > limit:
            return None
        return body

    return metadata, head_object, read_object_bytes
