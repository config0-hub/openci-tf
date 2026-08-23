# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Constant tables defining the bounded manifest schema (entry names, sets, bounds)."""
from __future__ import annotations

from src.domain.engine.artifact_limits import (
    MAX_BINARY_PLAN_BYTES,
    MAX_CHECKSUM_SIDECAR_BYTES,
    MAX_DONE_MARKER_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PLAN_METADATA_BYTES,
    MAX_RAW_ARTIFACT_BYTES,
)

_MAX_MANIFEST_ENTRIES = 64
_ALLOWED_ENTRY_NAMES = frozenset(
    {
        "init.out",
        "validate.out",
        "tf/plan.out",
        "drift.json",
        "tfsec.json",
        "infracost.json",
        "done",
        "package",
        "plan.tfplan",
        "plan.tfplan.sha256",
        "plan-metadata.json",
        "destroy.plan.tfplan",
        "destroy.plan.tfplan.sha256",
        "destroy-plan-metadata.json",
        "destroy.plan.out",
        "apply.out",
        "plan-show.out",
        "destroy.out",
    }
)
_ALLOWED_ENTRY_KEYS = frozenset({"name", "s3_uri", "content_type", "size", "checksum", "expires_at"})
_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "execution_id",
        "action",
        "generated_at",
        "manifest_s3_uri",
        "entries",
        "package_bucket",
        "tmp_bucket",
        "done_bucket",
        "plan_retention_days",
        "failure_reason",
        "manifest_sha256",
        "run_id",
        "repo_name",
        "commit_hash",
        "account_id",
        "folder",
        "attempt",
        "source_plan_run_id",
        "pr_number",
        "pointer_type",
    }
)
_TEXT_ARTIFACTS = {
    "init.out": "text/plain",
    "validate.out": "text/plain",
    "tf/plan.out": "text/plain",
    "drift.json": "application/json",
    "tfsec.json": "application/json",
    "infracost.json": "application/json",
    "destroy.plan.out": "text/plain",
    "apply.out": "text/plain",
    "plan-show.out": "text/plain",
    "destroy.out": "text/plain",
}
_SUCCESS_PLAN_REPORT_ENTRIES = frozenset(
    {
        "init.out",
        "validate.out",
        "tf/plan.out",
        "tfsec.json",
        "infracost.json",
        "done",
        "package",
        "plan.tfplan",
        "plan.tfplan.sha256",
        "plan-metadata.json",
    }
)
_SUCCESS_DRIFT_ENTRIES = frozenset(
    {
        "init.out",
        "validate.out",
        "tf/plan.out",
        "drift.json",
        "done",
        "package",
    }
)
_SUCCESS_PLAN_DESTROY_ENTRIES = frozenset(
    {
        "init.out",
        "validate.out",
        "destroy.plan.out",
        "destroy.plan.tfplan",
        "destroy.plan.tfplan.sha256",
        "destroy-plan-metadata.json",
        "done",
        "package",
    }
)
_SUCCESS_APPLY_ENTRIES = frozenset(
    {
        "init.out",
        "validate.out",
        "plan-show.out",
        "apply.out",
        "done",
        "package",
    }
)
_SUCCESS_DESTROY_ENTRIES = frozenset(
    {
        "init.out",
        "validate.out",
        "plan-show.out",
        "destroy.out",
        "done",
        "package",
    }
)
_FAILURE_PLAN_REPORT_ALLOWED = frozenset(
    {
        "init.out",
        "validate.out",
        "tf/plan.out",
        "tfsec.json",
        "infracost.json",
    }
)
_FAILURE_DRIFT_ALLOWED = frozenset(
    {
        "init.out",
        "validate.out",
        "tf/plan.out",
        "drift.json",
    }
)
_FAILURE_PLAN_DESTROY_ALLOWED = frozenset(
    {
        "init.out",
        "validate.out",
        "destroy.plan.out",
    }
)
_FAILURE_APPLY_ALLOWED = frozenset(
    {
        "init.out",
        "validate.out",
        "apply.out",
    }
)
_FAILURE_DESTROY_ALLOWED = frozenset(
    {
        "init.out",
        "validate.out",
        "destroy.out",
    }
)
_PACKAGE_CONTENT_TYPES = frozenset({"application/octet-stream", "application/zip"})
# The unmodified engine writes done JSON via put_object without ContentType; S3 stores binary/octet-stream.
_DONE_CONTENT_TYPES = frozenset({"binary/octet-stream", "application/octet-stream"})
_ENTRY_MAX_BYTES: dict[str, int] = {
    "init.out": MAX_RAW_ARTIFACT_BYTES,
    "validate.out": MAX_RAW_ARTIFACT_BYTES,
    "tf/plan.out": MAX_RAW_ARTIFACT_BYTES,
    "drift.json": MAX_RAW_ARTIFACT_BYTES,
    "tfsec.json": MAX_RAW_ARTIFACT_BYTES,
    "infracost.json": MAX_RAW_ARTIFACT_BYTES,
    "done": MAX_DONE_MARKER_BYTES,
    "plan-metadata.json": MAX_PLAN_METADATA_BYTES,
    "plan.tfplan": MAX_BINARY_PLAN_BYTES,
    "plan.tfplan.sha256": MAX_CHECKSUM_SIDECAR_BYTES,
    "destroy.plan.tfplan": MAX_BINARY_PLAN_BYTES,
    "destroy.plan.tfplan.sha256": MAX_CHECKSUM_SIDECAR_BYTES,
    "destroy-plan-metadata.json": MAX_PLAN_METADATA_BYTES,
    "destroy.plan.out": MAX_RAW_ARTIFACT_BYTES,
    "apply.out": MAX_RAW_ARTIFACT_BYTES,
    "plan-show.out": MAX_RAW_ARTIFACT_BYTES,
    "destroy.out": MAX_RAW_ARTIFACT_BYTES,
    "package": MAX_PACKAGE_BYTES,
}
# Per-name minimum byte sizes for schema validation. Text logs may be empty; JSON reports
# require at least two bytes; transfer objects and plan artifacts must be non-empty.
_ENTRY_MIN_BYTES: dict[str, int] = {
    "init.out": 0,
    "validate.out": 0,
    "tf/plan.out": 0,
    "drift.json": 2,
    "tfsec.json": 2,
    "infracost.json": 2,
    "done": 1,
    "package": 1,
    "plan.tfplan": 1,
    "plan.tfplan.sha256": 64,
    "plan-metadata.json": 2,
    "destroy.plan.tfplan": 1,
    "destroy.plan.tfplan.sha256": 64,
    "destroy-plan-metadata.json": 2,
    "destroy.plan.out": 0,
    "apply.out": 0,
    "plan-show.out": 0,
    "destroy.out": 0,
}
