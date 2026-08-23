# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write deterministic failure manifests and persist registry dimensions."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from src.core.logging import get_logger
from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_limits import MAX_MANIFEST_BYTES
from src.domain.engine.artifact_paths import expected_plan_artifact_uris
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.manifest import (
    build_failure_manifest,
    validate_manifest_binding,
    validate_manifest_schema,
)
from src.domain.engine.result import ExecutionResult
from src.domain.engine.run_artifact_layout import (
    manifest_key_for_layout,
    resolve_run_artifact_layout,
)
from src.domain.engine.summary import bounded_summary, validate_outer_child_output
from src.platform.aws.run_registry import put_folder_attempt
from src.platform.aws.s3 import get_bounded_json, put_json_create_only


logger = get_logger(__name__)

_ALLOWED_ACTIONS_BY_LANE = {
    "read": frozenset({"plan", "plan_destroy", "drift", "report"}),
    "apply": frozenset({"apply"}),
    "destroy": frozenset({"destroy"}),
}


def _unwrap_task_input(event: dict) -> tuple[dict, object | None]:
    wrapped = event.get("event")
    if isinstance(wrapped, dict):
        return wrapped, event.get("execution_started_at")
    return event, None


def _nested_payloads(event: dict) -> list[dict]:
    payloads = [event]
    for key in ("probe", "result"):
        value = event.get(key)
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def _failure_reason(event: dict) -> str:
    raw_reason: str = "execution failed"
    found = False
    for payload in _nested_payloads(event):
        for key in ("failure_reason", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                raw_reason = value.strip()
                found = True
                break
        if found:
            break
        error = payload.get("error")
        if isinstance(error, dict):
            cause = error.get("Cause") or error.get("cause")
            if isinstance(cause, str) and cause.strip():
                raw_reason = cause.strip()
                found = True
                break
    if not found:
        lane = os.environ.get("LANE_MODE")
        action = event.get("action")
        allowed_actions = _ALLOWED_ACTIONS_BY_LANE.get(lane) if lane is not None else None
        if (
            isinstance(action, str)
            and allowed_actions is not None
            and action not in allowed_actions
        ):
            raw_reason = (
                f"action {action} not allowed in {lane} lane; "
                f"allowed actions: {', '.join(sorted(allowed_actions))}"
            )
    bounded = redact_and_bound_terminal_evidence(raw_reason)
    if not isinstance(bounded, str):
        raise TypeError("failure manifest reason must be a string")
    return bounded


def _attempt(event: dict) -> int:
    for payload in _nested_payloads(event):
        if "attempt" in payload:
            return int(payload["attempt"])
    return 0


def _credential_expired(event: dict) -> bool:
    for payload in _nested_payloads(event):
        if payload.get("credential_expired") is True:
            return True
        error = payload.get("error")
        if isinstance(error, dict) and error.get("Error") == "CredentialExpiredError":
            return True
    return False


def _execution_id(event: dict) -> str:
    for payload in _nested_payloads(event):
        for key in ("exec_id", "execution_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    run_id = str(event.get("run_id") or "")
    folder = str(event.get("folder") or "")
    attempt = _attempt(event)
    if run_id and folder:
        return compose_execution_id(run_id, folder, attempt)
    raise ValueError("execution id is required for failure manifest")


def _parse_submitted_at(
    event: dict, execution_started_at: object | None = None
) -> datetime:
    for payload in _nested_payloads(event):
        raw = payload.get("submitted_at")
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).astimezone(timezone.utc)
    if isinstance(execution_started_at, (int, float)):
        return datetime.fromtimestamp(float(execution_started_at), tz=timezone.utc)
    if isinstance(execution_started_at, str) and execution_started_at.strip():
        text = execution_started_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    raise ValueError(
        "submitted_at or execution start time is required for deterministic failure manifest"
    )


def _persist_manifest(
    tmp_bucket: str,
    done_bucket: str,
    package_bucket: str,
    manifest_object_key: str,
    exec_id: str,
    manifest: dict,
) -> tuple[dict, bool]:
    key = manifest_object_key
    try:
        put_json_create_only(tmp_bucket, key, manifest)
        return manifest, False
    except ValueError:
        existing = get_bounded_json(tmp_bucket, key, MAX_MANIFEST_BYTES)
        if existing == manifest:
            return manifest, False
        if isinstance(existing, dict):
            validate_manifest_schema(existing, execution_id=exec_id)
            raw_attempt = manifest.get("attempt")
            if not isinstance(raw_attempt, int) or isinstance(raw_attempt, bool):
                raw_attempt = existing.get("attempt")
            attempt = raw_attempt if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool) else 0
            validate_manifest_binding(
                existing,
                run_id=str(manifest.get("run_id") or existing.get("run_id") or ""),
                repo_name=str(manifest.get("repo_name") or existing.get("repo_name") or ""),
                commit_hash=str(manifest.get("commit_hash") or existing.get("commit_hash") or ""),
                account_id=str(manifest.get("account_id") or existing.get("account_id") or ""),
                folder=str(manifest.get("folder") or existing.get("folder") or ""),
                action=str(manifest.get("action") or existing.get("action") or ""),
                attempt=attempt,
            )
            expected_manifest_uri = f"s3://{tmp_bucket}/{manifest_object_key}"
            if existing.get("manifest_s3_uri") != expected_manifest_uri:
                raise ValueError("committed manifest s3 uri does not match expected physical key")
            if existing.get("tmp_bucket") != tmp_bucket:
                raise ValueError("committed manifest tmp_bucket mismatch")
            if existing.get("done_bucket") != done_bucket:
                raise ValueError("committed manifest done_bucket mismatch")
            if existing.get("package_bucket") != package_bucket:
                raise ValueError("committed manifest package_bucket mismatch")
            if not existing.get("failure_reason"):
                return existing, True
            return existing, False
        raise


def _authoritative_summary(
    *,
    event: dict,
    manifest: dict,
    exec_id: str,
    action: str,
    tmp_bucket: str,
    done_bucket: str,
    account_id: str,
    folder: str,
    attempt: int,
    folder_keys,
    pr_number: int | None = None,
    pointer_type: str | None = None,
    include_manifest_fields: bool = True,
) -> tuple[dict[str, object], dict[str, object], str]:
    succeeded = not bool(manifest.get("failure_reason"))
    failure_reason = None if succeeded else str(manifest.get("failure_reason") or "execution failed")
    pointers = {
        "artifacts_prefix": f"s3://{tmp_bucket}/{folder_keys.prefix}",
        "done": f"s3://{done_bucket}/{exec_id}/done",
    }
    if action in {"plan", "report"}:
        expected = expected_plan_artifact_uris(
            bucket=tmp_bucket,
            repo_name=str(manifest.get("repo_name") or event.get("repo_name") or ""),
            run_id=str(manifest.get("run_id") or event.get("run_id") or ""),
            folder_path=folder,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        pointers["plan_metadata"] = expected.metadata
    summary = bounded_summary(
        ExecutionResult(exec_id, succeeded, [], failure_reason),
        pointers,
        credential_expired=_credential_expired(event) and not succeeded,
        attempt=attempt,
    )
    summary["manifest_s3_uri"] = manifest["manifest_s3_uri"]
    summary["manifest_sha256"] = manifest["manifest_sha256"]
    if not include_manifest_fields:
        summary.pop("manifest_s3_uri", None)
        summary.pop("manifest_sha256", None)
    validate_outer_child_output(
        summary,
        folder=folder,
        account_id=account_id,
        execution_id=exec_id,
    )
    if succeeded:
        outcome: dict[str, object] = {"succeeded": True}
        status = "succeeded"
    else:
        outcome = {
            "succeeded": False,
            "error": str(summary.get("error") or "execution failed"),
        }
        if _credential_expired(event):
            outcome["credential_expired"] = True
        status = "failed"
    return summary, outcome, status


def handler(event: dict, _context: object) -> dict:
    event, execution_started_at = _unwrap_task_input(event)
    logger.info(
        "write_failure_manifest handler invoked",
        extra={"run_id": event.get("run_id"), "folder": event.get("folder"), "action": event.get("action")},
    )
    tmp_bucket = os.environ["TMP_BUCKET_NAME"]
    done_bucket = os.environ["DONE_BUCKET_NAME"]
    package_bucket = os.environ.get("PACKAGE_BUCKET_NAME", tmp_bucket)
    run_id = str(event.get("run_id") or "")
    folder = str(event.get("folder") or "")
    action = str(event.get("action") or "plan")
    account_id = str(event.get("account_id") or "")
    attempt = _attempt(event)
    raw_deadline = event.get("deadline_at")
    deadline_at = raw_deadline if isinstance(raw_deadline, str) else None
    exec_id = _execution_id(event)
    failure_reason = _failure_reason(event)
    generated_at_source = _parse_submitted_at(event, execution_started_at)
    source_plan_run_id: str | None
    if action in {"apply", "destroy"}:
        raw_source_plan_run_id = event.get("source_plan_run_id")
        if not isinstance(raw_source_plan_run_id, str) or not raw_source_plan_run_id:
            raise ValueError("mutation failure manifest requires source_plan_run_id")
        source_plan_run_id = raw_source_plan_run_id
    else:
        source_plan_run_id = str(event["source_plan_run_id"]) if event.get("source_plan_run_id") else None
    repo_name = str(event.get("repo_name") or "")
    layout = resolve_run_artifact_layout(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        action=action,
    )
    manifest_object_key = manifest_key_for_layout(
        layout, repo_name=repo_name, run_id=run_id, folder_path=folder
    )
    manifest = build_failure_manifest(
        execution_id=exec_id,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        package_bucket=package_bucket,
        action=action,
        failure_reason=failure_reason,
        run_id=run_id,
        repo_name=repo_name,
        commit_hash=str(event.get("commit_hash") or ""),
        account_id=account_id,
        folder=folder,
        attempt=attempt,
        generated_at_source=generated_at_source,
        source_plan_run_id=source_plan_run_id,
        pr_number=layout.pr_number,
        pointer_type=layout.pointer_type,
        manifest_object_key=manifest_object_key,
    )
    # Validate the complete attempted output before the create-only S3 write.
    _authoritative_summary(
        event=event,
        manifest=manifest,
        exec_id=exec_id,
        action=action,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        account_id=account_id,
        folder=folder,
        attempt=attempt,
        folder_keys=layout.folder_keys,
        pr_number=layout.pr_number,
        pointer_type=layout.pointer_type,
    )
    registry_only = bool(event.get("registry_only"))
    if registry_only:
        summary, outcome, registry_status = _authoritative_summary(
            event=event,
            manifest=manifest,
            exec_id=exec_id,
            action=action,
            tmp_bucket=tmp_bucket,
            done_bucket=done_bucket,
            account_id=account_id,
            folder=folder,
            attempt=attempt,
            folder_keys=layout.folder_keys,
            pr_number=layout.pr_number,
            pointer_type=layout.pointer_type,
            include_manifest_fields=False,
        )
        summary["registry_outcome"] = outcome
        return summary
    manifest, _ = _persist_manifest(
        tmp_bucket,
        done_bucket,
        package_bucket,
        manifest_object_key,
        exec_id,
        manifest,
    )
    summary, outcome, registry_status = _authoritative_summary(
        event=event,
        manifest=manifest,
        exec_id=exec_id,
        action=action,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        account_id=account_id,
        folder=folder,
        attempt=attempt,
        folder_keys=layout.folder_keys,
        pr_number=layout.pr_number,
        pointer_type=layout.pointer_type,
    )
    if run_id and folder and os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        put_folder_attempt(
            run_id=run_id,
            folder=folder,
            account_id=account_id,
            execution_id=exec_id,
            attempt=attempt,
            status=registry_status,
            manifest_s3_uri=str(manifest["manifest_s3_uri"]),
            manifest_sha256=str(manifest["manifest_sha256"]),
            outcome=outcome,
            deadline_at=deadline_at,
        )
    logger.info("write_failure_manifest handler completed", extra={"run_id": run_id, "folder": folder, "action": action})
    return summary
