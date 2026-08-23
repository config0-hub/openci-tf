"""Normalize nested Map execution outcomes for renderer and finalizer."""
from __future__ import annotations

from typing import Any


def normalize_map_outcome(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten outer Map item shape into bounded folder outcome fields."""
    folder = raw.get("folder", "unknown")
    execution_id = raw.get("execution_id")
    account_id = raw.get("account_id")
    output = raw.get("output", raw)
    step_index = raw.get("step_index")
    if not isinstance(output, dict):
        result: dict[str, Any] = {
            "folder": folder,
            "status": "infrastructure_error",
            "error": "inner execution returned invalid output",
        }
        if isinstance(execution_id, str):
            result["execution_id"] = execution_id
        if isinstance(account_id, str):
            result["account_id"] = account_id
        if isinstance(step_index, int) and not isinstance(step_index, bool):
            result["step_index"] = step_index
        return result
    result = {
        **output,
        "folder": raw.get("folder", output.get("folder", "unknown")),
        "execution_id": output.get("exec_id") or output.get("execution_id") or execution_id,
    }
    output_step_index = output.get("step_index")
    if isinstance(output_step_index, int) and not isinstance(output_step_index, bool):
        result["step_index"] = output_step_index
    elif isinstance(step_index, int) and not isinstance(step_index, bool):
        result["step_index"] = step_index
    if isinstance(account_id, str):
        result["account_id"] = account_id
    return result
