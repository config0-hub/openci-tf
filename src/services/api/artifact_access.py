"""Presign and URI-confinement security helpers for artifact access."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.domain.engine.artifact_paths import (
    build_folder_artifact_keys,
    expected_plan_artifact_uris,
    manifest_key,
)


def _expected_manifest_uri(tmp_bucket: str, repo_name: str, run_id: str, folder: str) -> str:
    return f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}"


def _artifact_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed <= datetime.now(timezone.utc)


def _presign_ttl(expires_at: str | None, *, default: int = 900) -> int:
    if not expires_at:
        return 0
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    remaining = int((parsed - datetime.now(timezone.utc)).total_seconds())
    if remaining <= 0:
        return 0
    return min(default, remaining)


def _authorized_entry_uri(
    *,
    name: str,
    uri: str,
    tmp_bucket: str,
    done_bucket: str,
    execution_id: str,
    run_id: str,
    run_record: dict[str, Any],
    folder_record: dict[str, Any],
) -> bool:
    if not uri.startswith("s3://"):
        return False
    bucket, key = _s3_parts(uri)
    folder = str(folder_record.get("folder") or "")
    repo_name = str(run_record.get("repo_name") or "")
    keys = build_folder_artifact_keys(repo_name=repo_name, run_id=run_id, folder_path=folder)
    key_by_name = {
        "init.out": keys.init_out,
        "validate.out": keys.validate_out,
        "tf/plan.out": keys.plan_out,
        "drift.json": keys.drift_json,
        "tfsec.json": keys.tfsec_json,
        "infracost.json": keys.infracost_json,
    }
    if bucket == tmp_bucket and name in key_by_name and key == key_by_name[name]:
        return True
    if bucket == done_bucket and key == f"{execution_id}/done" and name == "done":
        return True
    if name in {"plan.tfplan", "plan.tfplan.sha256", "plan-metadata.json"}:
        expected = expected_plan_artifact_uris(
            bucket=tmp_bucket,
            repo_name=repo_name,
            run_id=run_id,
            folder_path=folder,
        )
        allowed = {
            "plan.tfplan": expected.plan,
            "plan.tfplan.sha256": expected.checksum,
            "plan-metadata.json": expected.metadata,
        }
        return uri == allowed[name]
    if name == "package":
        return False
    return False


def _s3_parts(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError("invalid s3 uri")
    bucket, key = uri[5:].split("/", 1)
    if not bucket or not key:
        raise ValueError("invalid s3 uri")
    return bucket, key


def _confined_uri(
    uri: str,
    *,
    name: str,
    tmp_bucket: str,
    done_bucket: str,
    execution_id: str,
    run_id: str,
    run_record: dict[str, Any],
    folder_record: dict[str, Any],
) -> bool:
    return _authorized_entry_uri(
        name=name,
        uri=uri,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        execution_id=execution_id,
        run_id=run_id,
        run_record=run_record,
        folder_record=folder_record,
    )
