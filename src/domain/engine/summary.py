# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Step Functions-safe result summaries."""
from __future__ import annotations

import json

from src.core.errors import ConfigResolutionError
from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_limits import (
    MAX_OUTER_CHILD_ERROR_CHARS,
    MAX_OUTER_CHILD_ERROR_JSON_BYTES,
    MAX_OUTER_MAP_OUTCOME_BYTES,
)
from src.domain.engine.inner_state import serialized_state_bytes
from src.domain.engine.result import ExecutionResult

# Backward-compatible alias retained for tests documenting the complete-child cap seam.
MAX_SUMMARY_BYTES = MAX_OUTER_MAP_OUTCOME_BYTES


def bounded_error_text(error: str | None) -> str | None:
    """Backward-compatible error helper backed by the terminal evidence policy."""
    if error is None:
        return None
    bounded = redact_and_bound_terminal_evidence(error)
    if not isinstance(bounded, str):
        raise TypeError("terminal error redactor must return a string")
    if len(bounded) > MAX_OUTER_CHILD_ERROR_CHARS:
        raise ValueError("terminal error exceeds outer child character bound")
    # The shared redactor's encoded string bound is the same 260-byte ASL seam.
    if len(json.dumps(bounded, separators=(",", ":")).encode("utf-8")) > MAX_OUTER_CHILD_ERROR_JSON_BYTES:
        raise ValueError("terminal error exceeds outer child encoded bound")
    return bounded


def bounded_summary(
    result: ExecutionResult,
    pointers: dict[str, str],
    credential_expired: bool = False,
    attempt: int | None = None,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "exec_id": result.trigger_id,
        "succeeded": result.succeeded,
        "pointers": pointers,
    }
    bounded = bounded_error_text(result.error)
    if bounded is not None:
        summary["error"] = bounded
    if credential_expired:
        summary["credential_expired"] = True
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        summary["attempt"] = attempt
    return summary


def build_outer_map_outcome(
    *,
    folder: str,
    account_id: str,
    execution_id: str,
    output: dict[str, object] | None = None,
    status: str | None = None,
    error: str | None = None,
    attempt: int | None = None,
    succeeded: bool | None = None,
    step_index: int | None = None,
) -> dict[str, object]:
    """Build one outer Map outcome using the production merge or infrastructure-error shape."""
    if output is not None:
        outcome = {
            "folder": folder,
            "account_id": account_id,
            "execution_id": execution_id,
            "output": output,
        }
        if isinstance(step_index, int) and not isinstance(step_index, bool):
            outcome["step_index"] = step_index
        return outcome
    outcome: dict[str, object] = {
        "folder": folder,
        "account_id": account_id,
        "execution_id": execution_id,
        "status": status or "infrastructure_error",
        "succeeded": False if succeeded is None else succeeded,
        "error": bounded_error_text(error) or "infrastructure error",
    }
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        outcome["attempt"] = attempt
    if isinstance(step_index, int) and not isinstance(step_index, bool):
        outcome["step_index"] = step_index
    return outcome


def validate_outer_map_outcome(outcome: dict[str, object]) -> None:
    """Reject a single outer Map child outcome that cannot fit Step Functions state."""
    size = serialized_state_bytes(outcome)
    if size > MAX_OUTER_MAP_OUTCOME_BYTES:
        raise ConfigResolutionError(
            f"outer map outcome exceeds Step Functions budget ({size} bytes)"
        )


def validate_outer_child_output(
    output: dict[str, object],
    *,
    folder: str,
    account_id: str,
    execution_id: str,
) -> None:
    """Reject a complete child output once every required field has been attached."""
    validate_outer_map_outcome(
        build_outer_map_outcome(
            folder=folder,
            account_id=account_id,
            execution_id=execution_id,
            output=output,
        )
    )
