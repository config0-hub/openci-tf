"""Collect Lambda returns bounded state-machine-safe data and writes manifest."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

from src.core.logging import get_logger
from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_limits import MAX_MANIFEST_BYTES
from src.domain.engine.artifact_paths import pr_pointer_key
from src.domain.engine.run_artifact_layout import (
    manifest_key_for_layout,
    resolve_run_artifact_layout,
)
from src.domain.engine.manifest import (
    BucketSet,
    ManifestBinding,
    build_manifest,
    validate_manifest_binding,
    validate_manifest_schema,
)
from src.domain.engine.plan_artifacts import MAX_PLAN_METADATA_BYTES
from src.domain.engine.result import ExecutionResult
from src.domain.engine.summary import bounded_summary, validate_outer_child_output
from src.domain.engine.pointer_publish import publish_execution_pointer
from src.platform.aws.run_registry import put_folder_attempt
from src.platform.aws.run_registry.step_index import registry_step_index_from_state
from src.platform.aws.s3 import (
    copy_object,  # noqa: F401 - retained as a unit-test compatibility seam
    get_bounded_json,
    get_object_bytes,
    head_object,
    put_json_create_only,
)

logger = get_logger(__name__)

_MAX_COLLECT_ATTEMPTS = 3
_MAX_DRIFT_RESULT_BYTES = 1_024


def _put_pointer_object(
    *, bucket: str, key: str, body: bytes, if_match: str | None = None
) -> None:
    import boto3

    params: dict[str, object] = {
        "Bucket": bucket,
        "Key": key,
        "Body": body,
        "ContentType": "text/plain",
    }
    if if_match is not None:
        params["IfMatch"] = if_match
    boto3.client("s3").put_object(**params)


def _submitted_at(event: dict) -> datetime | None:
    raw = event.get("submitted_at")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    return None


def _validate_existing_manifest_against_event(
    existing: dict,
    *,
    exec_id: str,
    run_id: str,
    repo_name: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    action: str,
    attempt: int,
    tmp_bucket: str,
    done_bucket: str,
    package_bucket: str,
    manifest_object_key: str,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> None:
    validate_manifest_schema(
        existing,
        execution_id=exec_id,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    validate_manifest_binding(
        existing,
        run_id=run_id,
        repo_name=repo_name,
        commit_hash=commit_hash,
        account_id=account_id,
        folder=folder,
        action=action,
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


def _authoritative_from_existing(
    existing: dict,
    *,
    exec_id: str,
    run_id: str,
    repo_name: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    action: str,
    attempt: int,
    tmp_bucket: str,
    done_bucket: str,
    package_bucket: str,
    manifest_object_key: str,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> tuple[dict, bool, str | None]:
    _validate_existing_manifest_against_event(
        existing,
        exec_id=exec_id,
        run_id=run_id,
        repo_name=repo_name,
        commit_hash=commit_hash,
        account_id=account_id,
        folder=folder,
        action=action,
        attempt=attempt,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        package_bucket=package_bucket,
        manifest_object_key=manifest_object_key,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    if existing.get("failure_reason"):
        return existing, False, str(existing.get("failure_reason") or "execution failed")
    return existing, True, None


def _drift_result(
    manifest: dict,
    *,
    action: str,
    tmp_bucket: str,
) -> bool | None:
    if action != "drift":
        return None
    entry = next(
        (
            item
            for item in manifest.get("entries", [])
            if isinstance(item, dict) and item.get("name") == "drift.json"
        ),
        None,
    )
    if entry is None:
        return None
    uri = entry.get("s3_uri")
    if not isinstance(uri, str) or not uri.startswith(f"s3://{tmp_bucket}/"):
        raise ValueError("drift result has invalid registry binding")
    key = uri.removeprefix(f"s3://{tmp_bucket}/")
    body = get_object_bytes(tmp_bucket, key, _MAX_DRIFT_RESULT_BYTES)
    if body is None:
        raise ValueError("drift result is missing")
    checksum = entry.get("checksum")
    if not isinstance(checksum, str) or hashlib.sha256(body).hexdigest() != checksum:
        raise ValueError("drift result checksum mismatch")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("drift result is malformed JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"drift"} or type(payload.get("drift")) is not bool:
        raise ValueError("drift result must contain exactly one boolean drift field")
    return payload["drift"]


def _persist_manifest_registry(
    *,
    run_id: str,
    folder: str,
    account_id: str,
    exec_id: str,
    attempt: int,
    succeeded: bool,
    manifest: dict,
    error: str | None,
    credential_expired: bool,
    deadline_at: str | None = None,
    drift_detected: bool | None = None,
    step_index: int = 1,
) -> None:
    if not run_id or not folder or not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return
    outcome: dict[str, object] = {"succeeded": True}
    if not succeeded:
        outcome = {"succeeded": False, "error": error}
        if credential_expired:
            outcome["credential_expired"] = True
    put_folder_attempt(
        run_id=run_id,
        folder=folder,
        account_id=account_id,
        execution_id=exec_id,
        attempt=attempt,
        status="succeeded" if succeeded else "failed",
        manifest_s3_uri=str(manifest["manifest_s3_uri"]),
        manifest_sha256=str(manifest["manifest_sha256"]),
        outcome=outcome,
        deadline_at=deadline_at,
        drift_detected=drift_detected,
        step_index=step_index,
    )


def _expected_plan_metadata_key(action: str, folder_keys: object) -> str:
    if action == "plan_destroy":
        return str(getattr(folder_keys, "destroy_plan_metadata"))
    return str(getattr(folder_keys, "plan_metadata"))


def handler(event: dict, _context: object) -> dict:
    logger.info(
        "collect handler invoked",
        extra={"run_id": event.get("run_id"), "folder": event.get("folder"), "exec_id": event.get("exec_id")},
    )
    tmp_bucket = os.environ["TMP_BUCKET_NAME"]
    done_bucket = os.environ["DONE_BUCKET_NAME"]
    package_bucket = os.environ.get("PACKAGE_BUCKET_NAME", "")
    exec_id = event["exec_id"]
    action = str(event.get("action") or "plan")
    raw_pointers = event.get("pointers")
    pointers: dict[str, str] = {}
    if isinstance(raw_pointers, dict):
        for key, value in raw_pointers.items():
            if isinstance(key, str) and isinstance(value, str):
                pointers[key] = value
    account_id = str(event.get("account_id") or "")
    folder = str(event.get("folder") or "")
    repo_name = str(event.get("repo_name") or "")
    commit_hash = str(event.get("commit_hash") or "")
    run_id = str(event.get("run_id") or "")
    attempt = int(event.get("attempt") or 0)
    raw_deadline = event.get("deadline_at")
    deadline_at = raw_deadline if isinstance(raw_deadline, str) else None
    plan_metadata_uri = event.get("plan_metadata_uri")
    if not isinstance(plan_metadata_uri, str):
        plan_metadata_uri = pointers.get("plan_metadata")
    plan_metadata = None
    layout = resolve_run_artifact_layout(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        action=action,
    )
    folder_keys = layout.folder_keys
    if action in {"plan", "report", "plan_destroy"} and isinstance(plan_metadata_uri, str):
        expected_plan_metadata_key = _expected_plan_metadata_key(action, folder_keys)
        if plan_metadata_uri != f"s3://{tmp_bucket}/{expected_plan_metadata_key}":
            raise ValueError("plan metadata uri does not match expected canonical key")
        _, key = plan_metadata_uri[5:].split("/", 1)
        plan_metadata = get_bounded_json(tmp_bucket, key, MAX_PLAN_METADATA_BYTES)
    done_uri = pointers.get("done") or f"s3://{done_bucket}/{exec_id}/done"
    package_uri = f"s3://{package_bucket}/{exec_id}.zip" if package_bucket else None
    failure_reason = None
    if not event.get("succeeded"):
        bounded_failure = redact_and_bound_terminal_evidence(
            event.get("error") or "execution failed"
        )
        if not isinstance(bounded_failure, str):
            raise TypeError("collect terminal error must be a string")
        failure_reason = bounded_failure
    manifest_object_key = manifest_key_for_layout(
        layout, repo_name=repo_name, run_id=run_id, folder_path=folder
    )
    manifest = build_manifest(
        execution_id=exec_id,
        buckets=BucketSet(
            tmp_bucket=tmp_bucket,
            done_bucket=done_bucket,
            package_bucket=package_bucket or tmp_bucket,
            done_uri=done_uri,
            package_uri=package_uri,
        ),
        binding=ManifestBinding(
            run_id=run_id,
            repo_name=repo_name,
            commit_hash=commit_hash,
            account_id=account_id,
            folder=folder,
            attempt=attempt,
            source_plan_run_id=str(event["source_plan_run_id"]) if event.get("source_plan_run_id") else None,
            pr_number=layout.pr_number,
            pointer_type=layout.pointer_type,
        ),
        action=action,
        head_object=head_object,
        read_object_bytes=get_object_bytes,
        plan_metadata=plan_metadata,
        plan_dimensions={
            "repo_name": repo_name,
            "commit_hash": commit_hash,
            "account_id": account_id,
            "folder": folder,
            "run_id": run_id,
        },
        failure_reason=failure_reason,
        generated_at_source=_submitted_at(event),
        folder_keys=folder_keys,
        manifest_object_key=manifest_object_key,
    )
    pointers = dict(pointers)
    pointers["artifacts_prefix"] = f"s3://{tmp_bucket}/{folder_keys.prefix}"
    authoritative_manifest = manifest
    authoritative_succeeded = bool(event["succeeded"])
    authoritative_error = None
    if not authoritative_succeeded:
        bounded_error = redact_and_bound_terminal_evidence(
            event.get("error") or "execution failed"
        )
        if not isinstance(bounded_error, str):
            raise TypeError("collect terminal error must be a string")
        authoritative_error = bounded_error
    result = ExecutionResult(exec_id, authoritative_succeeded, event.get("steps", []), authoritative_error)
    summary = bounded_summary(
        result,
        pointers,
        credential_expired=bool(event.get("credential_expired")),
        attempt=event.get("attempt"),
    )
    drift_detected = _drift_result(
        authoritative_manifest,
        action=action,
        tmp_bucket=tmp_bucket,
    )
    summary["manifest_s3_uri"] = authoritative_manifest["manifest_s3_uri"]
    summary["manifest_sha256"] = authoritative_manifest["manifest_sha256"]
    summary["succeeded"] = authoritative_succeeded
    if drift_detected is not None:
        summary["drift_detected"] = drift_detected
    bounded_error = summary.get("error")
    validate_outer_child_output(
        summary,
        folder=folder,
        account_id=account_id,
        execution_id=exec_id,
    )
    key = manifest_object_key
    last_error: Exception | None = None
    for attempt_no in range(_MAX_COLLECT_ATTEMPTS):
        try:
            put_json_create_only(tmp_bucket, key, manifest)
            break
        except ValueError:
            existing = get_bounded_json(tmp_bucket, key, MAX_MANIFEST_BYTES)
            if existing == manifest:
                break
            if isinstance(existing, dict):
                authoritative_manifest, authoritative_succeeded, authoritative_error = _authoritative_from_existing(
                    existing,
                    exec_id=exec_id,
                    run_id=run_id,
                    repo_name=repo_name,
                    commit_hash=commit_hash,
                    account_id=account_id,
                    folder=folder,
                    action=action,
                    attempt=attempt,
                    tmp_bucket=tmp_bucket,
                    done_bucket=done_bucket,
                    package_bucket=package_bucket or tmp_bucket,
                    manifest_object_key=manifest_object_key,
                    pr_number=layout.pr_number,
                    pointer_type=layout.pointer_type,
                )
                result = ExecutionResult(
                    exec_id, authoritative_succeeded, event.get("steps", []), authoritative_error
                )
                summary = bounded_summary(
                    result,
                    pointers,
                    credential_expired=bool(event.get("credential_expired")),
                    attempt=event.get("attempt"),
                )
                drift_detected = _drift_result(
                    authoritative_manifest,
                    action=action,
                    tmp_bucket=tmp_bucket,
                )
                summary["manifest_s3_uri"] = authoritative_manifest["manifest_s3_uri"]
                summary["manifest_sha256"] = authoritative_manifest["manifest_sha256"]
                summary["succeeded"] = authoritative_succeeded
                if drift_detected is not None:
                    summary["drift_detected"] = drift_detected
                bounded_error = summary.get("error")
                validate_outer_child_output(
                    summary,
                    folder=folder,
                    account_id=account_id,
                    execution_id=exec_id,
                )
                break
            raise
        except (RuntimeError, OSError) as error:
            last_error = error
            if attempt_no + 1 < _MAX_COLLECT_ATTEMPTS:
                time.sleep(0.2 * (attempt_no + 1))
    else:
        raise RuntimeError("collect manifest persistence failed") from last_error
    if (
        authoritative_succeeded
        and layout.pr_number is not None
        and layout.pointer_type is not None
        and action in {"plan", "plan_destroy", "report", "apply", "destroy"}
    ):
        publish_execution_pointer(
            bucket=tmp_bucket,
            key=pr_pointer_key(
                repo_name=repo_name,
                pr_number=layout.pr_number,
                folder_path=folder,
                pointer_type=layout.pointer_type,
            ),
            execution_id=run_id,
            head_object=head_object,
            put_text=_put_pointer_object,
            get_text=lambda bucket, key: get_object_bytes(bucket, key, max_bytes=128),
        )
    registry_error: Exception | None = None
    for attempt_no in range(_MAX_COLLECT_ATTEMPTS):
        try:
            _persist_manifest_registry(
                run_id=run_id,
                folder=folder,
                account_id=account_id,
                exec_id=exec_id,
                attempt=attempt,
                succeeded=authoritative_succeeded,
                manifest=authoritative_manifest,
                error=str(bounded_error) if bounded_error is not None else None,
                credential_expired=bool(summary.get("credential_expired")),
                deadline_at=deadline_at,
                drift_detected=drift_detected,
                step_index=registry_step_index_from_state(event.get("step_index")),
            )
            registry_error = None
            break
        except (RuntimeError, OSError, ValueError) as error:
            registry_error = error
            if attempt_no + 1 < _MAX_COLLECT_ATTEMPTS:
                time.sleep(0.2 * (attempt_no + 1))
    if registry_error is not None:
        raise RuntimeError("collect registry reconciliation failed") from registry_error
    logger.info("collect handler completed", extra={"run_id": run_id, "folder": folder, "exec_id": exec_id})
    return summary
