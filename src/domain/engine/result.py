"""Strict parsing for engine done markers."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from src.core.errors import MalformedResultError, TriggerMismatchError
from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_limits import (
    MAX_DONE_MARKER_ERROR_CHARS,
    MAX_ENGINE_EXIT_CODE,
    MAX_INNER_STEP_COUNT,
    MAX_POLL_DONE_RESULT_BYTES,
    MAX_STEP_METADATA_STRING_CHARS,
    MIN_ENGINE_EXIT_CODE,
)

_EXPIRY_SIGNATURES = ("expiredtoken", "expired token", "security token included in the request is expired")
_ENGINE_STEP_NAME = "step-0"
_TOP_LEVEL_CONTRACT_KEYS = frozenset({"trigger_id", "status", "steps", "error"})
_STEP_CONTRACT_KEYS = frozenset({"step_name", "status", "exit_code", "duration_seconds", "output"})
_STEP_STATUSES = frozenset({"succeeded", "failed"})
_CURL_PROGRESS_RE = re.compile(r"^\s*\d+\s+\d+.*--:--:--")
_NOISE_PREFIXES = ("% Total", "Dload", "Upload", "Speed", "Current")
_ACTIONABLE_RE = re.compile(r"(?i)(failed|fatal|not found|not set|permission denied|exit code)")


def has_credential_expiry_signature(message: str) -> bool:
    return any(signature in message.lower() for signature in _EXPIRY_SIGNATURES)


def _sanitize_error_line(line: str) -> str:
    redacted = redact_and_bound_terminal_evidence(line)
    if not isinstance(redacted, str):
        raise TypeError("terminal error redactor must return a string")
    return redacted


def _is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _CURL_PROGRESS_RE.match(stripped):
        return True
    if stripped.startswith(_NOISE_PREFIXES):
        return True
    return stripped.startswith("{") and stripped.endswith("}")


def _failed_step_outputs(steps: list) -> list[tuple[dict, str]]:
    outputs: list[tuple[dict, str]] = []
    for step in steps:
        if not isinstance(step, dict) or step.get("status") != "failed":
            continue
        output = step.get("output")
        if isinstance(output, str) and output.strip():
            outputs.append((step, output))
    return outputs


def derive_error_from_steps(steps: list) -> str | None:
    """Derive a bounded error from failed step output when the done marker omits one."""
    outputs = _failed_step_outputs(steps)
    if not outputs:
        return None
    combined = "\n".join(output for _, output in outputs)
    lines = combined.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Error:") or "AccessDenied" in stripped:
            return _sanitize_error_line(stripped)
    for line in reversed(lines):
        stripped = line.strip()
        if stripped and not _is_noise_line(stripped) and _ACTIONABLE_RE.search(stripped):
            return _sanitize_error_line(stripped)
    for line in reversed(lines):
        stripped = line.strip()
        if stripped and not _is_noise_line(stripped):
            return _sanitize_error_line(stripped)
    for step, _ in reversed(outputs):
        exit_code = step.get("exit_code")
        if type(exit_code) is int:
            return f"step failed with exit code {exit_code}"
    return None


def _bounded_metadata_string(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise MalformedResultError(f"done marker step {field} must be a string")
    if len(value) > MAX_STEP_METADATA_STRING_CHARS:
        raise MalformedResultError(f"done marker step {field} exceeds length limit")
    return value


def _validate_exit_code(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise MalformedResultError("malformed done marker step exit_code")
    if value < MIN_ENGINE_EXIT_CODE or value > MAX_ENGINE_EXIT_CODE:
        raise MalformedResultError("malformed done marker step exit_code")
    return value


def _validate_duration_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedResultError("malformed done marker step duration_seconds")
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise MalformedResultError("malformed done marker step duration_seconds")
    return duration


def _validate_contract_step(step: object, *, top_status: str) -> dict:
    if not isinstance(step, dict):
        raise MalformedResultError("malformed done marker")
    unknown = set(step.keys()) - _STEP_CONTRACT_KEYS
    if unknown:
        raise MalformedResultError("malformed done marker step contains unknown fields")
    missing = sorted(_STEP_CONTRACT_KEYS - step.keys())
    if missing:
        raise MalformedResultError("malformed done marker")
    step_name = _bounded_metadata_string("step_name", step["step_name"])
    if step_name != _ENGINE_STEP_NAME:
        raise MalformedResultError("malformed done marker step_name")
    step_status = step["status"]
    if not isinstance(step_status, str) or step_status not in _STEP_STATUSES:
        raise MalformedResultError("malformed done marker step status")
    exit_code = _validate_exit_code(step["exit_code"])
    duration_seconds = _validate_duration_seconds(step["duration_seconds"])
    output = step["output"]
    if not isinstance(output, str):
        raise MalformedResultError("malformed done marker step output")
    if step_status == "succeeded" and exit_code != 0:
        raise MalformedResultError("malformed done marker step exit_code")
    if step_status == "failed" and exit_code == 0:
        raise MalformedResultError("malformed done marker step exit_code")
    if top_status == "succeeded" and step_status != "succeeded":
        raise MalformedResultError("malformed done marker step status")
    if top_status == "failed" and step_status != "failed":
        raise MalformedResultError("malformed done marker step status")
    return {
        "step_name": step_name,
        "status": step_status,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "output": output,
    }


def _validate_steps(steps: object, *, status: str, error: str | None) -> list[dict]:
    if not isinstance(steps, list):
        raise MalformedResultError("malformed done marker")
    if len(steps) > MAX_INNER_STEP_COUNT:
        raise MalformedResultError("done marker step count exceeds engine contract")
    if not steps:
        if status == "failed" and isinstance(error, str) and error:
            return []
        raise MalformedResultError("malformed done marker")
    if len(steps) != MAX_INNER_STEP_COUNT:
        raise MalformedResultError("done marker step count does not match engine contract")
    return [_validate_contract_step(steps[0], top_status=status)]


@dataclass(frozen=True)
class ExecutionResult:
    trigger_id: str
    succeeded: bool
    steps: list[dict]
    error: str | None = None

    @property
    def credential_expired(self) -> bool:
        if self.succeeded:
            return False
        messages = [self.error or ""]
        messages.extend(
            step.get("output", "")
            for step in self.steps
            if isinstance(step, dict) and step.get("status") == "failed" and isinstance(step.get("output"), str)
        )
        return any(has_credential_expiry_signature(message) for message in messages)


def bound_step_metadata(steps: list) -> list[dict]:
    """Return typed step metadata without raw stdout for Step Functions state."""
    bound: list[dict] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        bound.append(
            {
                "step_name": _bounded_metadata_string("step_name", step["step_name"]),
                "status": _bounded_metadata_string("status", step["status"]),
                "exit_code": _validate_exit_code(step["exit_code"]),
            }
        )
    return bound


def bound_poll_done_payload(payload: dict[str, object]) -> dict[str, object]:
    """Ensure a ProbeDone task result stays within the Step Functions-safe budget."""
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    if len(encoded) <= MAX_POLL_DONE_RESULT_BYTES:
        return payload
    error = payload.get("error")
    if isinstance(error, str) and len(error) > 256:
        trimmed = dict(payload)
        trimmed["error"] = error[:253] + "..."
        encoded = json.dumps(trimmed, separators=(",", ":")).encode()
        if len(encoded) <= MAX_POLL_DONE_RESULT_BYTES:
            return trimmed
    raise ValueError("poll done result exceeds state size budget")


def parse_result(value: object, expected_execution_id: str) -> ExecutionResult:
    if not isinstance(value, dict):
        raise MalformedResultError("done marker must be an object")
    unknown = set(value.keys()) - _TOP_LEVEL_CONTRACT_KEYS
    if unknown:
        raise MalformedResultError("malformed done marker")
    trigger_id = value.get("trigger_id")
    if not isinstance(trigger_id, str) or trigger_id != expected_execution_id:
        raise TriggerMismatchError("done marker trigger_id mismatch")
    status, steps = value.get("status"), value.get("steps")
    has_error_key = "error" in value
    error = value.get("error")
    if status not in {"succeeded", "failed"}:
        raise MalformedResultError("malformed done marker")
    if status == "succeeded":
        if has_error_key:
            raise MalformedResultError("malformed done marker")
        error = None
    elif has_error_key:
        if not isinstance(error, str) or not error:
            raise MalformedResultError("malformed done marker")
        if len(error) > MAX_DONE_MARKER_ERROR_CHARS:
            raise MalformedResultError("done marker error exceeds length limit")
        error = redact_and_bound_terminal_evidence(error)
        if not isinstance(error, str):
            raise MalformedResultError("malformed done marker error")
    elif error is not None:
        raise MalformedResultError("malformed done marker")
    validated_steps = _validate_steps(steps, status=status, error=error if isinstance(error, str) else None)
    if status == "failed" and not error:
        error = derive_error_from_steps(validated_steps)
    return ExecutionResult(trigger_id, status == "succeeded", validated_steps, error)