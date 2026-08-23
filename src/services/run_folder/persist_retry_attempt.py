# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Atomically construct retry evidence, persist attempt zero, and resubmit once."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from src.core.logging import get_logger
from src.platform.aws.run_registry import put_folder_attempt
from src.services.run_folder import write_failure_manifest

logger = get_logger(__name__)


@dataclass(frozen=True)
class CredentialRetry:
    """Typed credential-expiry retry request derived from one ASL execution."""

    state: dict[str, Any]
    attempt: int
    exec_id: str | None
    submitted_at: object

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "CredentialRetry":
        state, execution_started_at = _unwrap_task_input(event)
        attempt, execution = _attempt_source(state)
        exec_id = _optional_execution_id(execution)
        submitted_at = _submitted_at(execution, state, execution_started_at)
        _validate_retry_state(state, attempt)
        return cls(
            state=state,
            attempt=attempt,
            exec_id=exec_id,
            submitted_at=submitted_at,
        )

    def manifest_event(self) -> dict[str, Any]:
        manifest_event = deepcopy(self.state)
        manifest_event["attempt"] = self.attempt
        manifest_event["submitted_at"] = self.submitted_at
        manifest_event["credential_expired"] = True
        manifest_event["failure_reason"] = "credential expired before retry"
        manifest_event["registry_only"] = True
        if self.exec_id is not None:
            manifest_event["exec_id"] = self.exec_id
        return manifest_event

    def resubmit_state(self) -> dict[str, Any]:
        state = deepcopy(self.state)
        for transient in ("result", "probe", "error", "retry_manifest"):
            state.pop(transient, None)
        state["attempt"] = self.attempt + 1
        # Preserve deterministic evidence time if the resubmitted Prepare task itself
        # encounters credential expiry before it can produce a result envelope.
        state["submitted_at"] = self.submitted_at
        return state


def _unwrap_task_input(event: dict[str, Any]) -> tuple[dict[str, Any], object | None]:
    wrapped = event.get("event")
    if isinstance(wrapped, dict):
        return deepcopy(wrapped), event.get("execution_started_at")
    return deepcopy(event), None


def _attempt_source(state: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    probe = state.get("probe")
    if isinstance(probe, dict) and "attempt" in probe:
        return int(probe["attempt"]), probe
    result = state.get("result")
    if isinstance(result, dict) and "attempt" in result:
        return int(result["attempt"]), result
    if "attempt" in state:
        return int(state["attempt"]), state
    raise ValueError("credential retry requires an attempt")


def _optional_execution_id(source: dict[str, Any]) -> str | None:
    for key in ("exec_id", "execution_id"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _submitted_at(
    execution: dict[str, Any], state: dict[str, Any], execution_started_at: object | None
) -> object:
    if "submitted_at" in execution:
        return execution["submitted_at"]
    if "submitted_at" in state:
        return state["submitted_at"]
    if execution_started_at is not None:
        return execution_started_at
    raise ValueError("credential retry requires submitted_at or execution start time")


def _validate_retry_state(state: dict[str, Any], attempt: int) -> None:
    if attempt != 0:
        raise ValueError(f"credential retry only permits attempt 0, found {attempt}")
    required = ("run_id", "folder", "action", "account_id", "repo_name", "commit_hash")
    missing = [key for key in required if not state.get(key)]
    if missing:
        raise ValueError(f"credential retry missing required fields: {', '.join(missing)}")
    action = state["action"]
    if action in {"apply", "destroy"}:
        source_plan_run_id = state.get("source_plan_run_id")
        if not isinstance(source_plan_run_id, str) or not source_plan_run_id:
            raise ValueError("mutation credential retry requires source_plan_run_id")
    elif action in {"plan", "plan_destroy", "drift", "report"}:
        return
    else:
        raise ValueError(f"credential retry received unsupported action: {action}")


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Persist retry evidence before returning the only allowed resubmit envelope."""
    retry = CredentialRetry.from_event(event)
    logger.info(
        "persist_retry_attempt handler invoked",
        extra={"run_id": retry.state.get("run_id"), "folder": retry.state.get("folder"), "action": "retry"},
    )
    summary = write_failure_manifest.handler(retry.manifest_event(), _context)
    outcome = summary.get("registry_outcome")
    if not isinstance(outcome, dict):
        raise ValueError("credential retry manifest did not return a registry outcome")
    execution_id = summary.get("exec_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise ValueError("credential retry manifest did not return an execution id")
    put_folder_attempt(
        run_id=str(retry.state["run_id"]),
        folder=str(retry.state["folder"]),
        account_id=str(retry.state["account_id"]),
        execution_id=execution_id,
        attempt=retry.attempt,
        status="failed",
        manifest_s3_uri=None,
        manifest_sha256=None,
        outcome=outcome,
        deadline_at=(
            retry.state.get("deadline_at")
            if isinstance(retry.state.get("deadline_at"), str)
            else None
        ),
    )
    logger.info(
        "persist_retry_attempt handler completed",
        extra={"run_id": retry.state.get("run_id"), "folder": retry.state.get("folder")},
    )
    return retry.resubmit_state()
