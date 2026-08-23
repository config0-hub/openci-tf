# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Binary plan artifact metadata validation for the run-scoped layout."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.domain.engine.artifact_paths import (
    expected_destroy_plan_artifact_uris,
    expected_plan_artifact_uris,
    validate_run_id,
)

MAX_PLAN_METADATA_BYTES = 4096
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")


def plan_retention_days() -> int:
    raw = os.environ.get("PLAN_RETENTION_DAYS", "1")
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"PLAN_RETENTION_DAYS must be a positive integer, got {raw!r}") from error
    if value < 1:
        raise ValueError(f"PLAN_RETENTION_DAYS must be at least 1, got {value}")
    return value


def _utc_instant(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} must be a valid UTC timestamp") from error
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{label} must be UTC")
    return parsed


def validate_plan_artifact_metadata(
    *,
    metadata: dict[str, Any],
    bucket: str,
    repo_name: str,
    run_id: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    action: str,
    expected_tf_runtime: str | None = None,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> dict[str, Any]:
    """Validate a binary-plan metadata sidecar against the pinned run dimensions."""
    if action not in {"plan", "report", "plan_destroy"}:
        raise ValueError("binary plan metadata is only valid for plan/report/plan_destroy")
    if not _FULL_SHA.fullmatch(commit_hash):
        raise ValueError("commit_hash must be a full 40-character git SHA")
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("account_id must be a 12-digit AWS account id")
    run = validate_run_id(run_id)
    if action == "plan_destroy":
        expected = expected_destroy_plan_artifact_uris(
            bucket=bucket,
            repo_name=repo_name,
            run_id=run,
            folder_path=folder,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        expected_fields: dict[str, Any] = {
            "repo": repo_name,
            "run_id": run,
            "pinned_sha": commit_hash.lower(),
            "account_id": account_id,
            "folder": folder,
            "action": action,
            "expires_after_days": plan_retention_days(),
            "plan_s3_uri": expected.plan,
            "sha256_s3_uri": expected.checksum,
            "metadata_s3_uri": expected.metadata,
        }
    else:
        expected = expected_plan_artifact_uris(
            bucket=bucket,
            repo_name=repo_name,
            run_id=run,
            folder_path=folder,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        expected_fields = {
            "repo": repo_name,
            "run_id": run,
            "pinned_sha": commit_hash.lower(),
            "account_id": account_id,
            "folder": folder,
            "action": action,
            "expires_after_days": plan_retention_days(),
            "plan_s3_uri": expected.plan,
            "sha256_s3_uri": expected.checksum,
            "metadata_s3_uri": expected.metadata,
        }
    for field, expected_value in expected_fields.items():
        actual = metadata.get(field)
        if field == "expires_after_days":
            if type(actual) is not int or actual != expected_value:
                raise ValueError(f"binary plan metadata {field} mismatch")
            continue
        if actual != expected_value:
            raise ValueError(f"binary plan metadata {field} mismatch")
    if not isinstance(metadata.get("opentofu_runtime"), str) or not metadata["opentofu_runtime"]:
        raise ValueError("binary plan metadata opentofu_runtime is required")
    if expected_tf_runtime is not None and metadata["opentofu_runtime"] != expected_tf_runtime:
        raise ValueError("binary plan metadata opentofu_runtime mismatch")
    checksum = metadata.get("sha256")
    if not isinstance(checksum, str) or not _CHECKSUM.fullmatch(checksum):
        raise ValueError("binary plan metadata sha256 must be 64 lowercase hex characters")
    created_at = _utc_instant(metadata.get("created_at"), label="created_at")
    expires_at = _utc_instant(metadata.get("expires_at"), label="expires_at")
    if expires_at <= created_at:
        raise ValueError("binary plan metadata expires_at must be after created_at")
    if expires_at - created_at != timedelta(days=plan_retention_days()):
        raise ValueError("binary plan metadata expiration interval must match configured plan retention days")
    return metadata
