# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DynamoDB key layout and retention defaults for the run registry."""

from __future__ import annotations

import hashlib

from src.core.registry_schema import folder_opaque_key

DEFAULT_RUN_HISTORY_RETENTION_DAYS = 90
MAX_ATTEMPTS_PER_FOLDER = 8


def run_pk(run_id: str) -> str:
    return f"run#{run_id}"


def run_meta_sk() -> str:
    return "meta"


def folder_summary_sk(folder: str) -> str:
    return f"folder#{folder_opaque_key(folder)}"


def folder_attempt_sk(folder: str, attempt: int) -> str:
    return f"folder#{folder_opaque_key(folder)}#attempt#{attempt:04d}"


def folder_attempt_prefix(folder: str) -> str:
    return f"folder#{folder_opaque_key(folder)}#attempt#"


def folder_submission_sk(folder: str, attempt: int) -> str:
    return f"submission#{folder_opaque_key(folder)}#attempt#{attempt:04d}"


def idempotency_pk(trigger_id: str) -> str:
    return f"idem#{trigger_id}"


def folder_gate_pk() -> str:
    return "folder-gates"


def folder_gate_sk(repo_name: str, folder: str) -> str:
    identity = f"{len(repo_name)}:{repo_name}{folder}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def repo_gsi_pk(trigger_id: str) -> str:
    return f"repo#{trigger_id}"


def repo_gsi_sk(created_at: int, run_id: str) -> str:
    return f"{created_at:020d}#{run_id}"


def pipeline_checkpoint_gsi_pk(
    *,
    trigger_id: str,
    repo_name: str,
    pipeline: str,
    action: str,
    step_index: int,
) -> str:
    if not trigger_id or not repo_name or not pipeline:
        raise ValueError("pipeline checkpoint GSI identity fields are required")
    if action not in {"apply", "destroy"}:
        raise ValueError("pipeline checkpoint action must be apply or destroy")
    if type(step_index) is not int or step_index < 1:
        raise ValueError("step_index must be an integer >= 1")
    identity = "|".join(
        (
            f"trigger={len(trigger_id)}:{trigger_id}",
            f"repo={len(repo_name)}:{repo_name}",
            f"pipeline={len(pipeline)}:{pipeline}",
            f"action={action}",
            f"step={step_index}",
        )
    )
    return f"pipeline-checkpoint#{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def pipeline_apply_gsi_pk(
    *,
    trigger_id: str,
    repo_name: str,
    pipeline: str,
    step_index: int,
) -> str:
    return pipeline_checkpoint_gsi_pk(
        trigger_id=trigger_id,
        repo_name=repo_name,
        pipeline=pipeline,
        action="apply",
        step_index=step_index,
    )


def pipeline_apply_gsi_sk(completed_at: int, run_id: str) -> str:
    if type(completed_at) is not int or completed_at < 0:
        raise ValueError("completed_at must be a non-negative integer")
    if not run_id:
        raise ValueError("run_id is required")
    return f"{completed_at:020d}#{run_id}"


def terminal_rank(status: str) -> int:
    ranks = {
        "accepted": 0,
        "running": 1,
        "succeeded": 2,
        "failed": 3,
        "infrastructure_error": 3,
        "in_progress": 1,
        "skipped": 2,
    }
    return ranks.get(status, 1)
