# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behavioral helpers mirroring rendered inner run-folder ASL data transformations."""
from __future__ import annotations

from typing import Any

_COLLECT_BASE_FIELDS = (
    "exec_id",
    "attempt",
    "succeeded",
    "credential_expired",
    "steps",
    "error",
    "pointers",
    "action",
    "repo_name",
    "commit_hash",
    "account_id",
    "folder",
    "run_id",
    "submitted_at",
)


def collect_task_parameters(state: dict[str, Any], *, mutation: bool) -> dict[str, Any]:
    """Mirror lane-specialized Collect task parameters after ProbeDone."""
    result = state["probe"]
    params = {
        "exec_id": result["exec_id"],
        "attempt": result["attempt"],
        "succeeded": result["succeeded"],
        "credential_expired": result["credential_expired"],
        "steps": result["steps"],
        "error": result["error"],
        "pointers": result["pointers"],
        "action": state["action"],
        "repo_name": state["repo_name"],
        "commit_hash": state["commit_hash"],
        "account_id": state["account_id"],
        "folder": state["folder"],
        "run_id": state["run_id"],
        "submitted_at": result["submitted_at"],
    }
    if mutation:
        params["source_plan_run_id"] = state["source_plan_run_id"]
    build_id = result.get("codebuild_build_id")
    if isinstance(build_id, str) and build_id:
        params["codebuild_build_id"] = build_id
    return params


def route_after_placeholder_failure(action: str) -> str:
    """Mirror RenderPlaceholder catch routing for mutation concurrency."""
    if action in {"apply", "destroy"}:
        return "RunFoldersSequential"
    return "NextStep"
