# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public DynamoDB run-registry API.

The package facade exposes registry operations, registry exceptions, retention
helpers, and the few schema constants still imported through this public path.
Patchable implementation hooks live on sibling modules such as
``run_registry._shared`` and ``run_registry.runs``; they are not re-exported here.
"""

from __future__ import annotations

from .keys import DEFAULT_RUN_HISTORY_RETENTION_DAYS, folder_submission_sk

from ._shared import (
    IdempotencyConflictError,
    RunRegistryError,
    RunRegistryQueryError,
    expire_ttl,
    is_expired,
)
from .folders import (
    get_folder_attempt,
    get_folder_record,
    list_folder_records,
    put_folder_attempt,
    put_folder_record,
    put_folder_submission,
    record_folder_submission_notification,
)
from .queries import (
    find_latest_successful_pipeline_apply,
    find_latest_successful_pipeline_checkpoint,
    list_folder_gate_projections,
    list_runs_authorized,
    list_runs_for_repo,
    put_folder_gate_observations,
)
from .runs import (
    attach_sfn_execution_arn,
    claim_idempotent_run,
    finalize_run_if_running,
    get_idempotency,
    get_run,
    mark_pipeline_apply_succeeded,
    mark_pipeline_checkpoint_succeeded,
    set_run_deadline,
    set_run_pipeline_metadata,
    update_run_status,
)

__all__ = [
    "DEFAULT_RUN_HISTORY_RETENTION_DAYS",
    "IdempotencyConflictError",
    "RunRegistryError",
    "RunRegistryQueryError",
    "attach_sfn_execution_arn",
    "claim_idempotent_run",
    "expire_ttl",
    "finalize_run_if_running",
    "find_latest_successful_pipeline_apply",
    "find_latest_successful_pipeline_checkpoint",
    "folder_submission_sk",
    "get_folder_attempt",
    "get_folder_record",
    "get_idempotency",
    "get_run",
    "is_expired",
    "list_folder_gate_projections",
    "list_folder_records",
    "list_runs_authorized",
    "list_runs_for_repo",
    "mark_pipeline_apply_succeeded",
    "mark_pipeline_checkpoint_succeeded",
    "put_folder_attempt",
    "put_folder_gate_observations",
    "put_folder_record",
    "put_folder_submission",
    "record_folder_submission_notification",
    "set_run_deadline",
    "set_run_pipeline_metadata",
    "update_run_status",
]
