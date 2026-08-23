"""Single-shot done-marker probe for the explicit Step Functions poll loop."""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from botocore.exceptions import ClientError

from src.core.logging import get_logger
from src.core.errors import (
    CredentialExpiredError,
    DoneMarkerTooLargeError,
    MalformedResultError,
    TriggerMismatchError,
)
from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_limits import MAX_DONE_MARKER_BYTES
from src.domain.engine.result import (
    bound_poll_done_payload,
    bound_step_metadata,
    has_credential_expiry_signature,
    parse_result,
)
from src.platform.aws import engine
from src.platform.aws.s3 import get_bounded_json_with_meta

logger = get_logger(__name__)

_CLOCK_SKEW = timedelta(seconds=5)


@dataclass(frozen=True)
class ProbeInput:
    """Validated execution identity and deadline for one bounded probe."""

    exec_id: str
    attempt: int
    submitted_at: float
    baseline_version_id: str | None
    plan_metadata_uri: str | None
    codebuild_build_id: str | None
    deadline_at: float

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "ProbeInput":
        prepared = event.get("result")
        if isinstance(prepared, dict) and isinstance(prepared.get("exec_id"), str):
            source = prepared
        elif isinstance(event.get("exec_id"), str):
            source = event
        else:
            raise ValueError("probe requires a prepared execution result")

        exec_id = source.get("exec_id")
        if not isinstance(exec_id, str) or not exec_id:
            raise ValueError("probe exec_id must be a non-empty string")
        submitted_at = float(source["submitted_at"])
        previous_probe = event.get("probe")
        previous_build_id = None
        if (
            isinstance(previous_probe, dict)
            and previous_probe.get("exec_id") == exec_id
        ):
            previous_build_id = _optional_string(previous_probe.get("codebuild_build_id"))
        return cls(
            exec_id=exec_id,
            attempt=int(source["attempt"]),
            submitted_at=submitted_at,
            baseline_version_id=_optional_string(source.get("done_baseline_version_id")),
            plan_metadata_uri=_optional_string(source.get("plan_metadata_uri")),
            codebuild_build_id=previous_build_id
            or _optional_string(source.get("codebuild_build_id")),
            deadline_at=_resolve_deadline_at(
                event, source=source, submitted_at=submitted_at
            ),
        )


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _parse_deadline(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("deadline_at must be an epoch number or ISO-8601 timestamp")
    if isinstance(value, (int, float)):
        deadline = float(value)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError("deadline_at timestamp must include a timezone")
        deadline = parsed.timestamp()
    else:
        raise ValueError("deadline_at must be an epoch number or ISO-8601 timestamp")
    if not math.isfinite(deadline):
        raise ValueError("deadline_at must be finite")
    return deadline


def _resolve_deadline_at(
    event: dict[str, Any], *, source: dict[str, Any], submitted_at: float
) -> float:
    """Use the execution-context deadline when supplied, otherwise the legacy budget."""
    if "deadline_at" in event:
        return _parse_deadline(event["deadline_at"])
    folder_context = event.get("folder_execution_context")
    if isinstance(folder_context, dict) and "deadline_at" in folder_context:
        return _parse_deadline(folder_context["deadline_at"])
    execution_context = event.get("execution_context")
    if isinstance(execution_context, dict) and "deadline_at" in execution_context:
        return _parse_deadline(execution_context["deadline_at"])
    # Compatibility path until every outer map item carries deadline_at.
    raw_budget = event.get("budget", source.get("budget"))
    if raw_budget is None:
        raise ValueError("probe requires deadline_at or a legacy budget")
    return submitted_at + int(raw_budget)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_stale_marker(
    *,
    last_modified: datetime,
    submitted_at: float,
    baseline_version_id: str | None,
    current_version_id: str | None,
) -> bool:
    if baseline_version_id is not None and current_version_id is not None and baseline_version_id == current_version_id:
        return True
    submitted = datetime.fromtimestamp(submitted_at, tz=timezone.utc)
    return _aware_utc(last_modified) < submitted - _CLOCK_SKEW


def _stale_note(
    *,
    last_modified: datetime,
    submitted_at: float,
    baseline_version_id: str | None,
    current_version_id: str | None,
) -> str:
    return (
        f"version={current_version_id or 'null'} baseline={baseline_version_id or 'null'} "
        f"last_modified={_aware_utc(last_modified).isoformat()} submitted_at={submitted_at}"
    )


def _load_done_marker(bucket: str, key: str) -> tuple[dict | None, dict | None]:
    try:
        return get_bounded_json_with_meta(bucket, key, MAX_DONE_MARKER_BYTES)
    except ValueError as error:
        message = str(error)
        if "exceeds" in message and "bytes" in message:
            raise DoneMarkerTooLargeError(message) from error
        raise MalformedResultError(message) from error
    except TypeError as error:
        raise MalformedResultError(str(error)) from error


def _resolve_codebuild_build_id(exec_id: str, existing: str | None) -> str | None:
    if existing is not None:
        return existing
    project_name = os.environ.get("ENGINE_CODEBUILD_PROJECT_NAME", "")
    if not project_name:
        return None
    return engine.resolve_codebuild_build_id(project_name, exec_id, max_attempts=1)


def _pending(probe: ProbeInput, *, reason: str, codebuild_build_id: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "probe_status": "pending",
        "exec_id": probe.exec_id,
        "attempt": probe.attempt,
        "submitted_at": probe.submitted_at,
        "pending_reason": reason,
    }
    if codebuild_build_id is not None:
        payload["codebuild_build_id"] = codebuild_build_id
    return payload


def _expired(probe: ProbeInput) -> dict[str, Any]:
    return {
        "probe_status": "expired",
        "exec_id": probe.exec_id,
        "attempt": probe.attempt,
        "submitted_at": probe.submitted_at,
        "credential_expired": False,
        "failure_reason": f"done marker deadline exceeded for {probe.exec_id}",
    }


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    """Perform one S3 observation; never sleep or loop inside Lambda."""
    probe = ProbeInput.from_event(event)
    logger.info("poll_done handler invoked", extra={"exec_id": probe.exec_id, "attempt": probe.attempt})
    if time.time() >= probe.deadline_at:
        return _expired(probe)

    codebuild_build_id = _resolve_codebuild_build_id(probe.exec_id, probe.codebuild_build_id)
    try:
        marker, meta = _load_done_marker(
            os.environ["DONE_BUCKET_NAME"], f"{probe.exec_id}/done"
        )
    except ClientError as error:
        if has_credential_expiry_signature(str(error)):
            raise CredentialExpiredError("done-marker credentials expired") from error
        raise

    if marker is None or meta is None:
        return _pending(probe, reason="marker_absent", codebuild_build_id=codebuild_build_id)

    current_version_id = _optional_string(meta.get("version_id"))
    last_modified = meta["last_modified"]
    if _is_stale_marker(
        last_modified=last_modified,
        submitted_at=probe.submitted_at,
        baseline_version_id=probe.baseline_version_id,
        current_version_id=current_version_id,
    ):
        return _pending(
            probe,
            reason=_stale_note(
                last_modified=last_modified,
                submitted_at=probe.submitted_at,
                baseline_version_id=probe.baseline_version_id,
                current_version_id=current_version_id,
            ),
            codebuild_build_id=codebuild_build_id,
        )

    try:
        result = parse_result(marker, probe.exec_id)
    except TriggerMismatchError:
        return _pending(probe, reason="trigger_mismatch", codebuild_build_id=codebuild_build_id)

    pointers = {
        "artifacts_prefix": f"s3://{os.environ['TMP_BUCKET_NAME']}/{probe.exec_id}/",
        "done": f"s3://{os.environ['DONE_BUCKET_NAME']}/{probe.exec_id}/done",
    }
    if probe.plan_metadata_uri is not None:
        pointers["plan_metadata"] = probe.plan_metadata_uri
    terminal_error = redact_and_bound_terminal_evidence(result.error)
    if terminal_error is not None and not isinstance(terminal_error, str):
        raise TypeError("done-marker terminal error must be a string")
    payload: dict[str, Any] = {
        "exec_id": probe.exec_id,
        "attempt": probe.attempt,
        "submitted_at": probe.submitted_at,
        "succeeded": result.succeeded,
        "error": terminal_error,
        "credential_expired": result.credential_expired,
        "steps": bound_step_metadata(result.steps),
        "pointers": pointers,
    }
    if codebuild_build_id is not None:
        payload["codebuild_build_id"] = codebuild_build_id
    bounded = bound_poll_done_payload(payload)
    bounded["probe_status"] = "complete" if result.succeeded else "terminal"
    logger.info("poll_done handler completed", extra={"exec_id": probe.exec_id, "status_code": bounded["probe_status"]})
    return bounded
