# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lookup fresh successful plan runs for apply/destroy intent gates."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from src.domain.engine.manifest import (
    validate_manifest_binding,
    validate_manifest_schema,
)
from src.domain.engine.artifact_limits import (
    MAX_BINARY_PLAN_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_PLAN_METADATA_BYTES,
)
from src.domain.engine.artifact_paths import pointer_type_for_action
from src.domain.engine.artifact_paths import (
    manifest_key,
    parse_execution_pointer,
    pr_pointer_key,
)
from src.domain.engine.plan_artifacts import validate_plan_artifact_metadata
from src.platform.aws.s3 import get_bounded_json, get_object_bytes, head_object
from src.platform.aws.run_registry import get_folder_record, get_run, list_runs_for_repo


def _pr_number(notification_target: object) -> int | None:
    if not isinstance(notification_target, dict):
        return None
    if notification_target.get("type") != "github_pr":
        return None
    pr_number = notification_target.get("pr_number")
    return pr_number if isinstance(pr_number, int) and pr_number > 0 else None


def _plan_action_for_mutation(action: str) -> str:
    if action == "apply":
        return "plan"
    if action == "destroy":
        return "plan_destroy"
    raise ValueError(f"unsupported mutation action: {action}")


def _plan_artifact_name(action: str) -> str:
    if action == "apply":
        return "plan.tfplan"
    if action == "destroy":
        return "destroy.plan.tfplan"
    raise ValueError(f"unsupported mutation action: {action}")


def _plan_metadata_artifact_name(required_plan_action: str) -> str:
    if required_plan_action == "plan_destroy":
        return "destroy-plan-metadata.json"
    return "plan-metadata.json"


def _metadata_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed <= datetime.now(timezone.utc)


def _manifest_entry(
    manifest: dict[str, Any], artifact_name: str
) -> dict[str, Any] | None:
    entries = manifest.get("entries") or []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("name") != artifact_name:
            continue
        expires_at = entry.get("expires_at")
        if _metadata_expired(expires_at if isinstance(expires_at, str) else None):
            return None
        checksum = entry.get("checksum")
        size = entry.get("size")
        if not isinstance(checksum, str) or len(checksum) != 64:
            return None
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None
        return entry
    return None


def _manifest_entry_checksum(
    manifest: dict[str, Any], artifact_name: str
) -> str | None:
    entry = _manifest_entry(manifest, artifact_name)
    if entry is None:
        return None
    checksum = entry.get("checksum")
    return checksum if isinstance(checksum, str) else None


def _folder_plan_sha256(
    run_id: str,
    folder: str,
    artifact_name: str,
    *,
    required_plan_action: str,
    commit_hash: str,
    account_id: str,
    expected_tf_runtime: str,
    repo_name: str,
    pr_number: int | None = None,
) -> str | None:
    record = get_folder_record(run_id, folder)
    if not record or record.get("status") != "succeeded":
        return None
    manifest_sha256 = record.get("manifest_sha256")
    if not isinstance(manifest_sha256, str):
        return None
    tmp_bucket = os.environ.get("TMP_BUCKET_NAME", "")
    if not tmp_bucket:
        return None
    run = get_run(run_id)
    if not run:
        return None
    resolved_repo = repo_name or str(run.get("repo_name") or "")
    manifest = get_bounded_json(
        tmp_bucket,
        manifest_key(
            resolved_repo,
            run_id,
            folder,
            pr_number=pr_number,
            pointer_type=pointer_type_for_action(required_plan_action)
            if pr_number is not None
            else None,
        ),
        MAX_MANIFEST_BYTES,
    )
    if not manifest:
        return None
    try:
        validate_manifest_schema(
            manifest,
            pr_number=pr_number,
            pointer_type=pointer_type_for_action(required_plan_action)
            if pr_number is not None
            else None,
        )
        validate_manifest_binding(
            manifest,
            run_id=run_id,
            repo_name=resolved_repo,
            commit_hash=commit_hash,
            account_id=account_id,
            folder=folder,
            action=required_plan_action,
            attempt=int(manifest.get("attempt") or 0),
        )
    except (TypeError, ValueError):
        return None
    if manifest.get("manifest_sha256") != manifest_sha256:
        return None
    checksum = _manifest_entry_checksum(manifest, artifact_name)
    if not checksum:
        return None
    metadata_name = _plan_metadata_artifact_name(required_plan_action)
    metadata_entry = _manifest_entry(manifest, metadata_name)
    if metadata_entry is None:
        return None
    metadata_uri = metadata_entry.get("s3_uri")
    if not isinstance(metadata_uri, str) or not metadata_uri.startswith("s3://"):
        return None
    metadata_bucket, metadata_key = metadata_uri[5:].split("/", 1)
    metadata_head = head_object(metadata_bucket, metadata_key)
    if metadata_head is None:
        return None
    metadata_size = int(metadata_head.get("content_length") or 0)
    if metadata_size != int(metadata_entry.get("size") or -1):
        return None
    if metadata_size > MAX_PLAN_METADATA_BYTES:
        return None
    metadata_body = get_object_bytes(
        metadata_bucket, metadata_key, MAX_PLAN_METADATA_BYTES
    )
    if metadata_body is None:
        return None
    metadata_checksum = hashlib.sha256(metadata_body).hexdigest()
    committed_metadata_checksum = metadata_entry.get("checksum")
    if metadata_checksum != committed_metadata_checksum:
        return None
    try:
        plan_metadata = json.loads(metadata_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(plan_metadata, dict):
        return None
    try:
        validate_plan_artifact_metadata(
            metadata=plan_metadata,
            bucket=tmp_bucket,
            repo_name=resolved_repo,
            run_id=run_id,
            commit_hash=commit_hash,
            account_id=account_id,
            folder=folder,
            action=required_plan_action,
            expected_tf_runtime=expected_tf_runtime,
            pr_number=pr_number,
            pointer_type=pointer_type_for_action(required_plan_action)
            if pr_number is not None
            else None,
        )
    except ValueError:
        return None
    for entry in manifest.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("name") != artifact_name:
            continue
        uri = entry.get("s3_uri")
        if not isinstance(uri, str) or not uri.startswith("s3://"):
            return None
        bucket, key = uri[5:].split("/", 1)
        meta = head_object(bucket, key)
        if meta is None:
            return None
        size = int(meta.get("content_length") or 0)
        if size > MAX_BINARY_PLAN_BYTES:
            return None
        body = get_object_bytes(bucket, key, MAX_BINARY_PLAN_BYTES)
        if body is None:
            return None
        if hashlib.sha256(body).hexdigest() != checksum:
            return None
        return checksum
    return None


def _plan_run_from_pointer(
    *,
    repo_name: str,
    pr_number: int,
    folder: str,
    mutation_action: str,
    commit_hash: str,
    account_id: str,
    expected_tf_runtime: str,
) -> dict[str, Any] | None:
    tmp_bucket = os.environ.get("TMP_BUCKET_NAME", "")
    if not tmp_bucket:
        return None
    required_plan_action = _plan_action_for_mutation(mutation_action)
    pointer_key = pr_pointer_key(
        repo_name=repo_name,
        pr_number=pr_number,
        folder_path=folder,
        pointer_type=pointer_type_for_action(required_plan_action),
    )
    pointer_body = get_object_bytes(tmp_bucket, pointer_key, max_bytes=128)
    if pointer_body is None:
        return None
    try:
        execution_id = parse_execution_pointer(pointer_body.decode("utf-8"))
    except ValueError:
        return None
    artifact_name = _plan_artifact_name(mutation_action)
    sha256 = _folder_plan_sha256(
        execution_id,
        folder,
        artifact_name,
        required_plan_action=required_plan_action,
        commit_hash=commit_hash,
        account_id=account_id,
        expected_tf_runtime=expected_tf_runtime,
        repo_name=repo_name,
        pr_number=pr_number,
    )
    if not sha256:
        return None
    return {
        "run_id": execution_id,
        "folder": folder,
        "plan_sha256": sha256,
        "plan_artifact_name": artifact_name,
        "tf_runtime": expected_tf_runtime,
    }


def find_newest_fresh_plan_run(
    *,
    trigger_id: str,
    repo_name: str,
    pr_number: int,
    folder: str,
    mutation_action: str,
    commit_hash: str,
    account_id: str,
    expected_tf_runtime: str,
    max_scan: int = 100,
) -> dict[str, Any] | None:
    """Return the newest successful plan run for a PR folder, or None."""
    required_plan_action = _plan_action_for_mutation(mutation_action)
    artifact_name = _plan_artifact_name(mutation_action)
    pointer_match = _plan_run_from_pointer(
        repo_name=repo_name,
        pr_number=pr_number,
        folder=folder,
        mutation_action=mutation_action,
        commit_hash=commit_hash,
        account_id=account_id,
        expected_tf_runtime=expected_tf_runtime,
    )
    if pointer_match is not None:
        return pointer_match
    cursor: str | None = None
    scanned = 0
    while scanned < max_scan:
        limit = min(25, max_scan - scanned)
        runs, cursor = list_runs_for_repo(trigger_id, limit=limit, cursor=cursor)
        scanned += len(runs)
        for run in runs:
            if run.get("status") != "succeeded":
                continue
            if str(run.get("action") or "") != required_plan_action:
                continue
            if str(run.get("commit_hash") or "").lower() != commit_hash.lower():
                continue
            if _pr_number(run.get("notification_target")) != pr_number:
                continue
            run_id = str(run.get("run_id") or "")
            if not run_id:
                continue
            folder_record = get_folder_record(run_id, folder)
            if not folder_record or folder_record.get("status") != "succeeded":
                continue
            sha256 = _folder_plan_sha256(
                run_id,
                folder,
                artifact_name,
                required_plan_action=required_plan_action,
                commit_hash=commit_hash,
                account_id=account_id,
                expected_tf_runtime=expected_tf_runtime,
                repo_name=repo_name,
                pr_number=pr_number,
            )
            if not sha256:
                continue
            return {
                "run_id": run_id,
                "folder": folder,
                "plan_sha256": sha256,
                "plan_artifact_name": artifact_name,
                "tf_runtime": expected_tf_runtime,
            }
        if not cursor:
            break
    return None
