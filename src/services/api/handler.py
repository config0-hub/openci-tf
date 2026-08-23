# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AWS IAM-protected core API routes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any
from urllib.parse import unquote

from src.core.logging import get_logger
from src.domain.engine.artifact_limits import (
    ALLOWED_ARTIFACT_CONTENT_TYPES,
    MAX_ARTIFACT_BYTES,
    MAX_BINARY_PLAN_BYTES,
    MAX_MANIFEST_BYTES,
)
from src.domain.engine.manifest import (
    validate_manifest_binding,
    validate_manifest_entry_name,
    validate_manifest_schema,
)
from src.domain.run.api_authorization import (
    ApiAuthorizationError,
    authorize_admin_read,
    authorize_artifact_read,
    authorize_create_run,
    authorize_list_runs,
    authorize_read_run,
)
from src.domain.run.folder_id import decode_folder_id, encode_folder_id, folder_matches
from src.domain.engine.artifact_paths import manifest_key
from src.domain.run.request import RunRequestValidationError, parse_run_request
from src.platform.aws.admin_registry import (
    AdminCursorError,
    list_account_targets,
    list_active_locks,
    list_repo_registrations,
)
from src.platform.aws.dynamo_codec import normalize_dynamo_value
from src.platform.aws.run_registry import (
    IdempotencyConflictError,
    RunRegistryQueryError,
    get_folder_record,
    get_run,
    list_folder_gate_projections,
    list_folder_records,
    list_runs_authorized,
)
from src.platform.aws.s3 import (
    get_bounded_json,
    get_bounded_text,
    get_object_bytes,
    head_object,
    presign_get,
)
from src.services.api.artifact_access import (
    _artifact_expired,
    _confined_uri,
    _expected_manifest_uri,
    _presign_ttl,
    _s3_parts,
)
from src.services.orchestration.start_run import (
    OrchestrationError,
    start_run_from_request,
)

logger = get_logger(__name__)

_MAX_INLINE_BYTES = 65_536
_RUN_ID = re.compile(r"^[A-Za-z0-9._=-]{1,128}$")


def _json_safe(value: Any) -> Any:
    return normalize_dynamo_value(value)


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(_json_safe(body))}


def _error_response(status: int, message: str, *, request_id: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": message}
    if request_id:
        payload["request_id"] = request_id
    return _response(status, payload)


def _api_step_index(raw_step_index: object) -> int:
    if type(raw_step_index) is int and raw_step_index >= 1:
        return raw_step_index
    elif raw_step_index is None:
        return 1
    else:
        raise ValueError("stored step_index must be an integer >= 1")


def _run_response_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    if not isinstance(payload.get("pipeline"), str) or not payload.get("pipeline"):
        payload.pop("pipeline", None)
        payload.pop("step_count", None)
    return payload


def _folder_response_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        row["step_index"] = _api_step_index(row.get("step_index"))
        folder = row.get("folder")
        if isinstance(folder, str):
            row["folder_id"] = encode_folder_id(folder)
        rows.append(row)
    return sorted(rows, key=lambda item: (item["step_index"], str(item.get("folder") or "")))


def _request_id() -> str:
    return uuid.uuid4().hex


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError("body must be a JSON object")
    return payload


def _route_key(event: dict[str, Any]) -> str:
    ctx = event.get("requestContext") or {}
    route = ctx.get("routeKey") or event.get("routeKey") or ""
    return str(route)


def _path_params(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("pathParameters") or {}
    return {str(k): unquote(str(v)) for k, v in raw.items()}


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("queryStringParameters") or {}
    return {str(k): str(v) for k, v in raw.items()}


def _resolve_folder(run_id: str, folder_param: str) -> tuple[str | None, str | None]:
    try:
        folder = decode_folder_id(folder_param)
    except ValueError:
        return None, "invalid folder_id"
    record = get_folder_record(run_id, folder)
    if not record or not folder_matches(folder_param, folder):
        return None, "folder execution not found or expired"
    return folder, None


def _create_run(event: dict[str, Any]) -> dict[str, Any]:
    request_id = _request_id()
    try:
        request = parse_run_request(_parse_body(event))
        authorize_create_run(event, trigger_id=request.trigger_id, action=request.action)
    except ApiAuthorizationError as error:
        return _error_response(403, str(error), request_id=request_id)
    except (RunRequestValidationError, ValueError, json.JSONDecodeError) as error:
        return _error_response(400, str(error), request_id=request_id)
    try:
        run_id, created = start_run_from_request(request)
    except IdempotencyConflictError as error:
        return _error_response(409, str(error), request_id=request_id)
    except OrchestrationError:
        return _error_response(502, "orchestration failed", request_id=request_id)
    return _response(202, {"run_id": run_id, "created": created})


def _get_run(event: dict[str, Any]) -> dict[str, Any]:
    run_id = _path_params(event).get("run_id", "")
    if not _RUN_ID.fullmatch(run_id):
        return _error_response(400, "invalid run_id")
    record = get_run(run_id)
    if not record:
        return _error_response(404, "run not found or expired")
    try:
        authorize_read_run(event, trigger_id=str(record.get("trigger_id") or ""), action=str(record.get("action") or ""))
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    return _response(200, _run_response_record(record))


def _list_runs(event: dict[str, Any]) -> dict[str, Any]:
    params = _query_params(event)
    trigger_id = params.get("trigger_id", "").strip() or None
    try:
        policy, trigger_ids = authorize_list_runs(event, trigger_id=trigger_id)
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    try:
        limit = int(params.get("limit", "25"))
    except ValueError:
        return _error_response(400, "limit must be an integer")
    try:
        items, cursor = list_runs_authorized(
            trigger_ids,
            actions=policy.actions,
            repo_filter=params.get("repo"),
            limit=limit,
            cursor=params.get("cursor"),
        )
    except RunRegistryQueryError as error:
        return _error_response(400, str(error))
    body: dict[str, Any] = {"runs": [_run_response_record(item) for item in items]}
    if cursor:
        body["cursor"] = cursor
    return _response(200, body)


def _admin_page_params(event: dict[str, Any]) -> tuple[int, str | None]:
    params = _query_params(event)
    try:
        limit = int(params.get("limit", "25"))
    except ValueError as error:
        raise ValueError("limit must be an integer") from error
    return limit, params.get("cursor")


def _admin_list_response(
    key: str,
    items: list[dict[str, Any]],
    cursor: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {key: items}
    if cursor:
        body["cursor"] = cursor
    return _response(200, body)


def _list_repos(event: dict[str, Any]) -> dict[str, Any]:
    try:
        authorize_admin_read(event)
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    try:
        limit, cursor = _admin_page_params(event)
    except ValueError as error:
        return _error_response(400, str(error))
    try:
        repos, next_cursor = list_repo_registrations(limit=limit, cursor=cursor)
    except AdminCursorError as error:
        return _error_response(400, str(error))
    return _admin_list_response("repos", repos, next_cursor)


def _list_accounts(event: dict[str, Any]) -> dict[str, Any]:
    try:
        authorize_admin_read(event)
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    try:
        limit, cursor = _admin_page_params(event)
    except ValueError as error:
        return _error_response(400, str(error))
    try:
        accounts, next_cursor = list_account_targets(limit=limit, cursor=cursor)
    except AdminCursorError as error:
        return _error_response(400, str(error))
    return _admin_list_response("accounts", accounts, next_cursor)


def _list_locks(event: dict[str, Any]) -> dict[str, Any]:
    try:
        authorize_admin_read(event)
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    try:
        limit, cursor = _admin_page_params(event)
    except ValueError as error:
        return _error_response(400, str(error))
    try:
        locks, next_cursor = list_active_locks(limit=limit, cursor=cursor)
    except AdminCursorError as error:
        return _error_response(400, str(error))
    return _admin_list_response("locks", locks, next_cursor)


def _get_gates(event: dict[str, Any]) -> dict[str, Any]:
    try:
        authorize_admin_read(event)
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    try:
        limit, cursor = _admin_page_params(event)
    except ValueError as error:
        return _error_response(400, str(error))
    try:
        folders, next_cursor = list_folder_gate_projections(limit=limit, cursor=cursor)
    except RunRegistryQueryError as error:
        return _error_response(400, str(error))
    # Apply enablement moved from an install-level flag to per-account
    # AccountAlias.enable_apply; the admin ledger reports the scope instead
    # of a single global boolean.
    body: dict[str, Any] = {
        "enable_apply": False,
        "enable_apply_scope": "per-account",
        "folders": folders,
        "folders_source": "latest-run-observation",
    }
    if next_cursor:
        body["cursor"] = next_cursor
    return _response(200, body)


def _list_folders(event: dict[str, Any]) -> dict[str, Any]:
    run_id = _path_params(event).get("run_id", "")
    if not _RUN_ID.fullmatch(run_id):
        return _error_response(400, "invalid run_id")
    record = get_run(run_id)
    if not record:
        return _error_response(404, "run not found or expired")
    try:
        authorize_read_run(event, trigger_id=str(record.get("trigger_id") or ""), action=str(record.get("action") or ""))
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    try:
        folders = _folder_response_rows(list_folder_records(run_id))
    except ValueError as error:
        return _error_response(500, str(error))
    return _response(200, {"folders": folders})


def _get_manifest(event: dict[str, Any]) -> dict[str, Any]:
    params = _path_params(event)
    run_id, folder_param = params.get("run_id", ""), params.get("folder", "")
    if not _RUN_ID.fullmatch(run_id):
        return _error_response(400, "invalid run_id")
    run_record = get_run(run_id)
    if not run_record:
        return _error_response(404, "run not found or expired")
    try:
        policy = authorize_read_run(event, trigger_id=str(run_record.get("trigger_id") or ""), action=str(run_record.get("action") or ""))
        authorize_artifact_read(policy, artifact_class="manifest", run_action=str(run_record.get("action") or ""))
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    folder, error = _resolve_folder(run_id, folder_param)
    if error:
        return _error_response(400, error)
    assert folder is not None
    record = get_folder_record(run_id, folder)
    if not record:
        return _error_response(404, "folder execution not found or expired")
    manifest_uri = record.get("manifest_s3_uri")
    execution_id = record.get("execution_id")
    if not isinstance(manifest_uri, str) or not isinstance(execution_id, str):
        return _error_response(404, "manifest not available")
    tmp_bucket = os.environ["TMP_BUCKET_NAME"]
    expected_uri = _expected_manifest_uri(tmp_bucket, str(run_record.get("repo_name") or ""), run_id, folder)
    if manifest_uri != expected_uri:
        return _error_response(404, "manifest not available")
    bucket, key = tmp_bucket, manifest_key(str(run_record.get("repo_name") or ""), run_id, folder)
    manifest = get_bounded_json(bucket, key, MAX_MANIFEST_BYTES)
    if manifest is None:
        return _error_response(404, "manifest object missing")
    try:
        validate_manifest_schema(manifest, execution_id=execution_id)
        validate_manifest_binding(
            manifest,
            run_id=run_id,
            repo_name=str(run_record.get("repo_name") or ""),
            commit_hash=str(run_record.get("commit_hash") or ""),
            account_id=str(record.get("account_id") or ""),
            folder=folder,
            action=str(run_record.get("action") or ""),
            attempt=int(record.get("attempt") or 0),
        )
    except (ValueError, TypeError):
        return _error_response(404, "manifest invalid")
    stored_digest = record.get("manifest_sha256")
    if not isinstance(stored_digest, str) or stored_digest != manifest.get("manifest_sha256"):
        return _error_response(404, "manifest digest mismatch")
    return _response(200, manifest)


def _get_artifact(event: dict[str, Any]) -> dict[str, Any]:
    params = _path_params(event)
    query = _query_params(event)
    run_id, folder_param = params.get("run_id", ""), params.get("folder", "")
    name = query.get("name", "").strip()
    if not _RUN_ID.fullmatch(run_id) or not validate_manifest_entry_name(name):
        return _error_response(400, "invalid run_id, folder, or artifact name")
    run_record = get_run(run_id)
    if not run_record:
        return _error_response(404, "run not found or expired")
    try:
        policy = authorize_read_run(event, trigger_id=str(run_record.get("trigger_id") or ""), action=str(run_record.get("action") or ""))
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    folder, error = _resolve_folder(run_id, folder_param)
    if error:
        return _error_response(400, error)
    assert folder is not None
    record = get_folder_record(run_id, folder)
    if not record:
        return _error_response(404, "folder execution not found or expired")
    execution_id = record.get("execution_id")
    if not isinstance(execution_id, str):
        return _error_response(404, "manifest not available")
    tmp_bucket = os.environ["TMP_BUCKET_NAME"]
    done_bucket = os.environ["DONE_BUCKET_NAME"]
    manifest = get_bounded_json(tmp_bucket, manifest_key(str(run_record.get("repo_name") or ""), run_id, folder), MAX_MANIFEST_BYTES)
    if not manifest:
        return _error_response(404, "manifest missing")
    try:
        validate_manifest_schema(manifest, execution_id=execution_id)
        validate_manifest_binding(
            manifest,
            run_id=run_id,
            repo_name=str(run_record.get("repo_name") or ""),
            commit_hash=str(run_record.get("commit_hash") or ""),
            account_id=str(record.get("account_id") or ""),
            folder=folder,
            action=str(run_record.get("action") or ""),
            attempt=int(record.get("attempt") or 0),
        )
    except (ValueError, TypeError):
        return _error_response(404, "manifest invalid")
    stored_digest = record.get("manifest_sha256")
    if not isinstance(stored_digest, str) or stored_digest != manifest.get("manifest_sha256"):
        return _error_response(404, "manifest digest mismatch")
    entries = manifest.get("entries") or []
    match = next((entry for entry in entries if isinstance(entry, dict) and entry.get("name") == name), None)
    if not match:
        return _error_response(404, "artifact not in manifest")
    uri = str(match.get("s3_uri") or "")
    if not _confined_uri(
        uri,
        name=name,
        tmp_bucket=tmp_bucket,
        done_bucket=done_bucket,
        execution_id=execution_id,
        run_id=run_id,
        run_record=run_record,
        folder_record=record,
    ):
        return _error_response(404, "artifact not available")
    if _artifact_expired(str(match.get("expires_at") or "") or None):
        return _error_response(410, "artifact expired")
    content_type = str(match.get("content_type") or "application/octet-stream")
    if name == "plan.tfplan":
        try:
            authorize_artifact_read(
                policy,
                artifact_class="binary_plan",
                binary_plan=True,
                run_action=str(run_record.get("action") or ""),
            )
            art_bucket, art_key = _s3_parts(uri)
            head = head_object(art_bucket, art_key)
            if head is None:
                return _error_response(404, "artifact missing")
            manifest_size = match.get("size")
            manifest_checksum = match.get("checksum")
            manifest_content_type = match.get("content_type")
            if isinstance(manifest_size, int) and int(head["content_length"]) != manifest_size:
                return _error_response(404, "artifact metadata mismatch")
            if isinstance(manifest_content_type, str) and head.get("content_type"):
                head_type = str(head["content_type"]).split(";", 1)[0]
                if head_type != manifest_content_type.split(";", 1)[0]:
                    return _error_response(404, "artifact metadata mismatch")
            if isinstance(manifest_checksum, str) and manifest_checksum:
                body = get_object_bytes(art_bucket, art_key, MAX_BINARY_PLAN_BYTES)
                if body is None:
                    return _error_response(404, "artifact missing")
                if hashlib.sha256(body).hexdigest() != manifest_checksum:
                    return _error_response(404, "artifact checksum mismatch")
            ttl = _presign_ttl(str(match.get("expires_at") or "") or None)
            if ttl <= 0:
                return _error_response(410, "artifact expired")
            url = presign_get(art_bucket, art_key, ttl)
            return _response(
                200,
                {
                    "download_url": url,
                    "expires_in": ttl,
                    "content_type": manifest_content_type if isinstance(manifest_content_type, str) else content_type,
                },
            )
        except ApiAuthorizationError as error:
            return _error_response(403, str(error))
        except ValueError:
            return _error_response(400, "invalid artifact uri")
    artifact_class = "json" if name.endswith(".json") else "text"
    try:
        authorize_artifact_read(policy, artifact_class=artifact_class, run_action=str(run_record.get("action") or ""))
    except ApiAuthorizationError as error:
        return _error_response(403, str(error))
    if content_type not in ALLOWED_ARTIFACT_CONTENT_TYPES and artifact_class != "json":
        return _error_response(403, "artifact type not exposed via inline read")
    try:
        art_bucket, art_key = _s3_parts(uri)
    except ValueError:
        return _error_response(400, "invalid artifact uri")
    if name.endswith(".json"):
        payload = get_bounded_json(art_bucket, art_key, MAX_ARTIFACT_BYTES)
        if payload is None:
            return _error_response(404, "artifact missing")
        manifest_size = match.get("size")
        manifest_checksum = match.get("checksum")
        manifest_content_type = match.get("content_type")
        head = head_object(art_bucket, art_key)
        if head is None:
            return _error_response(404, "artifact missing")
        if isinstance(manifest_size, int) and int(head["content_length"]) != manifest_size:
            return _error_response(404, "artifact metadata mismatch")
        if isinstance(manifest_content_type, str) and head.get("content_type"):
            head_type = str(head["content_type"]).split(";", 1)[0]
            if head_type != manifest_content_type.split(";", 1)[0]:
                return _error_response(404, "artifact metadata mismatch")
        if isinstance(manifest_checksum, str) and manifest_checksum:
            body = get_object_bytes(art_bucket, art_key, MAX_ARTIFACT_BYTES)
            if body is None:
                return _error_response(404, "artifact missing")
            if hashlib.sha256(body).hexdigest() != manifest_checksum:
                return _error_response(404, "artifact checksum mismatch")
        return _response(200, payload)
    text = get_bounded_text(art_bucket, art_key, MAX_ARTIFACT_BYTES, ALLOWED_ARTIFACT_CONTENT_TYPES)
    if text is None:
        return _error_response(404, "artifact missing")
    content_type, body = text
    manifest_size = match.get("size")
    manifest_checksum = match.get("checksum")
    manifest_content_type = match.get("content_type")
    head = head_object(art_bucket, art_key)
    if head is None:
        return _error_response(404, "artifact missing")
    if isinstance(manifest_size, int) and int(head["content_length"]) != manifest_size:
        return _error_response(404, "artifact metadata mismatch")
    if isinstance(manifest_content_type, str) and head.get("content_type"):
        head_type = str(head["content_type"]).split(";", 1)[0]
        if head_type != manifest_content_type.split(";", 1)[0]:
            return _error_response(404, "artifact metadata mismatch")
    if isinstance(manifest_checksum, str) and manifest_checksum:
        raw = get_object_bytes(art_bucket, art_key, MAX_ARTIFACT_BYTES)
        if raw is None:
            return _error_response(404, "artifact missing")
        if hashlib.sha256(raw).hexdigest() != manifest_checksum:
            return _error_response(404, "artifact checksum mismatch")
    if len(body.encode("utf-8")) > _MAX_INLINE_BYTES:
        return _error_response(413, "artifact exceeds inline bound")
    return _response(200, {"content_type": content_type, "body": body})


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    route = _route_key(event)
    logger.info("api handler invoked", extra={"route": route})
    if route == "POST /runs":
        return _create_run(event)
    if route == "GET /runs":
        return _list_runs(event)
    if route == "GET /runs/{run_id}":
        return _get_run(event)
    if route == "GET /runs/{run_id}/folders":
        return _list_folders(event)
    if route == "GET /runs/{run_id}/folders/{folder}/manifest":
        return _get_manifest(event)
    if route == "GET /runs/{run_id}/folders/{folder}/artifacts":
        return _get_artifact(event)
    if route == "GET /repos":
        return _list_repos(event)
    if route == "GET /accounts":
        return _list_accounts(event)
    if route == "GET /locks":
        return _list_locks(event)
    if route == "GET /gates":
        return _get_gates(event)
    return _error_response(404, f"unknown route: {route}")
