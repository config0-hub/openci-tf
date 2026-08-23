# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Artifact and S3 plumbing for render."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from src.domain.engine.artifact_limits import MAX_ARTIFACT_BYTES
from src.domain.engine.artifact_paths import (
    folder_artifact_prefix_for_run,
    report_all_pointer_key,
)
from src.domain.engine.outer_execution_id import validate_outer_run_id
from src.domain.engine.plan_artifacts import (
    MAX_PLAN_METADATA_BYTES,
    expected_plan_artifact_uris,
    validate_plan_artifact_metadata,
)
from src.domain.engine.pointer_publish import publish_execution_pointer
from src.domain.engine.artifact_paths import pointer_type_for_action
from src.platform.aws.s3 import get_bounded_json, get_object_bytes


def _s3_bucket_key(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


_PLAN_METADATA_DIMENSIONS_ERROR = "successful plan/report outcome missing dimensions for binary plan metadata validation"


def _requires_plan_artifact_metadata(outcome: dict[str, Any], action: str) -> bool:
    if action not in {"plan", "report"}:
        return False
    status = outcome.get("status")
    if status in {"failed", "infrastructure_error", "in_progress", "skipped"}:
        return False
    if outcome.get("credential_expired"):
        return False
    return outcome.get("succeeded") is not False


def _required_string(value: Any, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(message)
    return value


def _plan_artifact_metadata(
    outcome: dict[str, Any],
    action: str,
    webhook: dict[str, Any],
    run_id: str,
    *,
    pr_number: int | None = None,
) -> dict[str, Any] | None:
    if not _requires_plan_artifact_metadata(outcome, action):
        return None
    pointers = outcome.get("pointers")
    if not isinstance(pointers, dict):
        raise TypeError(
            "successful plan/report outcome missing binary plan metadata pointers"
        )
    metadata_uri = pointers.get("plan_metadata")
    if not isinstance(metadata_uri, str) or not metadata_uri:
        raise ValueError(
            "successful plan/report outcome missing binary plan metadata pointer"
        )
    repo_name = _required_string(
        webhook.get("repo_name"), _PLAN_METADATA_DIMENSIONS_ERROR
    )
    commit_hash = _required_string(
        webhook.get("commit_hash"), _PLAN_METADATA_DIMENSIONS_ERROR
    )
    account_id = _required_string(
        outcome.get("account_id"), _PLAN_METADATA_DIMENSIONS_ERROR
    )
    folder = _required_string(outcome.get("folder"), _PLAN_METADATA_DIMENSIONS_ERROR)
    bucket = os.environ["TMP_BUCKET_NAME"]
    scoped_pr, pointer_type = _scoped_pr_context(run_id, pr_number, action)
    expected = expected_plan_artifact_uris(
        bucket=bucket,
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=scoped_pr,
        pointer_type=pointer_type,
    )
    if metadata_uri != expected.metadata:
        raise ValueError("binary plan metadata pointer does not match expected run key")
    confined_bucket, confined_key = _s3_bucket_key(metadata_uri)
    if confined_bucket != bucket or metadata_uri != f"s3://{bucket}/{confined_key}":
        raise ValueError("binary plan metadata pointer is outside the tmp bucket")
    metadata = get_bounded_json(confined_bucket, confined_key, MAX_PLAN_METADATA_BYTES)
    if metadata is None:
        raise ValueError(f"missing binary plan metadata: {metadata_uri}")
    return validate_plan_artifact_metadata(
        metadata=metadata,
        bucket=bucket,
        repo_name=repo_name,
        run_id=run_id,
        commit_hash=commit_hash,
        account_id=account_id,
        folder=folder,
        action=action,
        pr_number=scoped_pr,
        pointer_type=pointer_type,
    )


def _scoped_pr_context(
    run_id: str, pr_number: int | None, action: str
) -> tuple[int | None, str | None]:
    if not isinstance(pr_number, int):
        return None, None
    try:
        validate_outer_run_id(run_id)
    except ValueError:
        return None, None
    return pr_number, pointer_type_for_action(action)


def _artifact_list_prefix(
    *,
    repo_name: str,
    run_id: str,
    folder: str,
    action: str,
    pr_number: int | None,
) -> str:
    scoped_pr, pointer_type = _scoped_pr_context(run_id, pr_number, action)
    return folder_artifact_prefix_for_run(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=scoped_pr,
        pointer_type=pointer_type,
    )


def _publish_report_all_pointer(
    *,
    repo_name: str,
    pr_number: int,
    run_id: str,
    terminal: str,
) -> None:
    if terminal != "succeeded":
        return
    bucket = os.environ.get("TMP_BUCKET_NAME", "")
    if not bucket:
        return
    from src.platform.aws.s3 import get_object_bytes, head_object

    def put_text(
        *, bucket: str, key: str, body: bytes, if_match: str | None = None
    ) -> None:
        import boto3

        params: dict[str, object] = {
            "Bucket": bucket,
            "Key": key,
            "Body": body,
            "ContentType": "text/plain",
        }
        if if_match is not None:
            params["IfMatch"] = if_match
        boto3.client("s3").put_object(**params)

    publish_execution_pointer(
        bucket=bucket,
        key=report_all_pointer_key(repo_name=repo_name, pr_number=pr_number),
        execution_id=run_id,
        head_object=head_object,
        put_text=put_text,
        get_text=lambda b, k: get_object_bytes(b, k, max_bytes=128),
    )


def _fetch_source_plan_text(
    *,
    repo_name: str,
    folder: str,
    action: str,
    source_run_id: str,
    pr_number: int | None,
) -> str | None:
    plan_key = "destroy.plan.out" if action == "destroy" else "tf/plan.out"
    pointer_type = "destroy" if action == "destroy" else "plan"
    prefix = _artifact_list_prefix(
        repo_name=repo_name,
        run_id=source_run_id,
        folder=folder,
        action="plan_destroy" if action == "destroy" else "plan",
        pr_number=pr_number,
    )
    key = f"{prefix}{plan_key}"
    bucket = os.environ.get("TMP_BUCKET_NAME", "")
    if not bucket:
        return None
    body = get_object_bytes(bucket, key, MAX_ARTIFACT_BYTES)
    if body is None:
        scoped_prefix = folder_artifact_prefix_for_run(
            repo_name=repo_name,
            run_id=source_run_id,
            folder_path=folder,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        body = get_object_bytes(bucket, f"{scoped_prefix}{plan_key}", MAX_ARTIFACT_BYTES)
    return body.decode("utf-8", errors="replace") if body else None
