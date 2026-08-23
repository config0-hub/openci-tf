# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transport-neutral run request validation for API and webhook ingress."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from src.core.registry_schema import normalize_folder_path
from src.domain.run.limits import (
    MAX_FOLDER_PATH_LENGTH,
    MAX_FOLDERS_PER_REQUEST,
    MAX_REQUEST_BODY_BYTES,
)

_API_ACTIONS = frozenset({"plan", "drift", "report"})
_SAFE_ACTIONS = frozenset({"plan", "drift", "report", "plan_destroy"})
_MUTATION_ACTIONS = frozenset({"apply", "destroy"})
_FORBIDDEN_ACTIONS: frozenset[str] = frozenset()
_FOLDER_MODES = frozenset({"affected", "all", "explicit", "pipeline"})
_NOTIFICATION_TYPES = frozenset({"github_pr", "registry"})
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_IDEM_KEY = re.compile(r"^[A-Za-z0-9._=-]{8,128}$")
_TRIGGER_ID = re.compile(r"^[A-Za-z0-9._=-]{1,128}$")
_FOLDER_PATH = re.compile(r"^[^/\\][^/\\]*(?:/[^/\\]+)*$")
_PIPELINE_NAME = re.compile(r"^[A-Za-z0-9_./-]+$")
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "trigger_id",
        "commit_hash",
        "action",
        "folder_mode",
        "folders",
        "pipeline",
        "pipeline_step",
        "idempotency_key",
        "notification_target",
    }
)
_ALLOWED_NOTIFICATION_KEYS = frozenset({"type", "pr_number"})
_FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "git_url",
        "role_arn",
        "assume_role_arn",
        "credentials",
        "s3_key",
        "s3_uri",
        "bucket",
        "package_uri",
        "execution_target",
        "ssm_path",
        "external_id",
    }
)


class RunRequestValidationError(ValueError):
    """Raised when a run request body is unsafe or incomplete."""


@dataclass
class NotificationTarget:
    type: str
    pr_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.pr_number is not None:
            payload["pr_number"] = self.pr_number
        return payload


@dataclass
class RunRequest:
    trigger_id: str
    commit_hash: str
    action: str
    folder_mode: str
    folders: list[str] = field(default_factory=list)
    pipeline: str | None = None
    pipeline_step: int | None = None
    idempotency_key: str = ""
    notification_target: NotificationTarget = field(default_factory=lambda: NotificationTarget("registry"))
    ingress_source: str = "api"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "commit_hash": self.commit_hash,
            "action": self.action,
            "folder_mode": self.folder_mode,
            "folders": list(self.folders),
            "pipeline": self.pipeline,
            "pipeline_step": self.pipeline_step,
            "idempotency_key": self.idempotency_key,
            "notification_target": self.notification_target.to_dict(),
            "ingress_source": self.ingress_source,
        }


def _reject_unknown_keys(body: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise RunRequestValidationError(f"unknown {label} fields: {', '.join(unknown)}")


def _reject_forbidden_keys(body: dict[str, Any]) -> None:
    for key in body:
        normalized = key.casefold().replace("-", "_")
        if normalized in _FORBIDDEN_INPUT_KEYS or key in _FORBIDDEN_INPUT_KEYS:
            raise RunRequestValidationError(f"caller-supplied field {key!r} is not allowed")


def _required_str(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunRequestValidationError(f"{field} is required")
    text = value.strip()
    if pattern is not None and not pattern.fullmatch(text):
        raise RunRequestValidationError(f"{field} is invalid")
    return text


def _canonical_pipeline_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunRequestValidationError("pipeline is required for pipeline folder_mode")
    pipeline = value.strip()
    if not _PIPELINE_NAME.fullmatch(pipeline):
        raise RunRequestValidationError("pipeline is invalid")
    if pipeline == "all":
        raise RunRequestValidationError("pipeline name 'all' is reserved")
    path = PurePosixPath(pipeline)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RunRequestValidationError("pipeline is invalid")
    return path.as_posix()


def _canonical_pipeline_step(value: object) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunRequestValidationError("pipeline_step must be an integer >= 1")
    if value < 1:
        raise RunRequestValidationError("pipeline_step must be an integer >= 1")
    return value


def _canonical_folders(raw_folders: list[Any]) -> list[str]:
    if len(raw_folders) > MAX_FOLDERS_PER_REQUEST:
        raise RunRequestValidationError(f"folders exceeds maximum of {MAX_FOLDERS_PER_REQUEST}")
    folders: list[str] = []
    seen: set[str] = set()
    for item in raw_folders:
        if not isinstance(item, str) or not item.strip():
            raise RunRequestValidationError("folders must contain only non-empty strings")
        try:
            folder = normalize_folder_path(item)
        except ValueError as error:
            raise RunRequestValidationError(str(error)) from error
        if len(folder.encode("utf-8")) > MAX_FOLDER_PATH_LENGTH:
            raise RunRequestValidationError("folder path exceeds maximum UTF-8 byte length")
        if not _FOLDER_PATH.fullmatch(folder):
            raise RunRequestValidationError(f"invalid folder path: {folder!r}")
        if folder in seen:
            continue
        seen.add(folder)
        folders.append(folder)
    return folders


def build_run_request(
    *,
    trigger_id: str,
    commit_hash: str,
    action: str,
    folder_mode: str,
    folders: list[str],
    idempotency_key: str,
    notification_target: NotificationTarget,
    ingress_source: str,
    pipeline: str | None = None,
    pipeline_step: int | None = None,
) -> RunRequest:
    """Validate and construct a RunRequest from normalized adapter inputs."""
    body = {
        "trigger_id": trigger_id,
        "commit_hash": commit_hash,
        "action": action,
        "folder_mode": folder_mode,
        "folders": folders,
        "pipeline": pipeline,
        "pipeline_step": pipeline_step,
        "idempotency_key": idempotency_key,
        "notification_target": notification_target.to_dict(),
    }
    request = parse_run_request(body, ingress_source=ingress_source)
    if request.notification_target.type != notification_target.type:
        raise RunRequestValidationError("notification_target mismatch")
    if request.notification_target.pr_number != notification_target.pr_number:
        raise RunRequestValidationError("notification_target mismatch")
    return request


def parse_run_request(body: dict[str, Any], *, ingress_source: str = "api") -> RunRequest:
    """Validate and normalize a run creation payload."""
    if not isinstance(body, dict):
        raise RunRequestValidationError("body must be a JSON object")
    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise RunRequestValidationError("request body exceeds maximum size")
    _reject_unknown_keys(body, _ALLOWED_TOP_LEVEL, "request")
    _reject_forbidden_keys(body)
    trigger_id = _required_str(body.get("trigger_id"), "trigger_id", _TRIGGER_ID)
    commit_hash = _required_str(body.get("commit_hash"), "commit_hash", _FULL_SHA).lower()
    action = _required_str(body.get("action"), "action").casefold()
    if action in _FORBIDDEN_ACTIONS:
        raise RunRequestValidationError(f"action {action!r} is permanently disabled")
    if action not in _SAFE_ACTIONS and action not in _MUTATION_ACTIONS:
        raise RunRequestValidationError("action must be one of plan, drift, report, plan_destroy, apply, or destroy")
    raw_pipeline = body.get("pipeline")
    raw_folder_mode = body.get("folder_mode")
    if raw_pipeline is not None and raw_folder_mode is None:
        folder_mode = "pipeline"
    else:
        folder_mode = _required_str(raw_folder_mode, "folder_mode").casefold()
    if folder_mode not in _FOLDER_MODES:
        raise RunRequestValidationError("folder_mode must be affected, all, explicit, or pipeline")
    if raw_pipeline is not None and folder_mode != "pipeline":
        raise RunRequestValidationError("folder_mode must be omitted or pipeline when pipeline is supplied")
    raw_folders = body.get("folders", [])
    if raw_folders is None:
        raw_folders = []
    if not isinstance(raw_folders, list):
        raise RunRequestValidationError("folders must be a list")
    folders = _canonical_folders(raw_folders)
    raw_pipeline_step = body.get("pipeline_step")
    pipeline: str | None = None
    pipeline_step: int | None = None
    if folder_mode == "pipeline":
        pipeline = _canonical_pipeline_name(raw_pipeline)
        if folders:
            raise RunRequestValidationError("pipeline is mutually exclusive with folders")
        if action == "destroy":
            raise RunRequestValidationError("destroy pipeline is not supported")
        if action == "report":
            raise RunRequestValidationError("report is not supported for pipelines")
        if action == "apply":
            pipeline_step = _canonical_pipeline_step(raw_pipeline_step)
        elif raw_pipeline_step is not None:
            raise RunRequestValidationError("pipeline_step is only valid for apply pipelines")
    else:
        if raw_pipeline is not None:
            raise RunRequestValidationError("pipeline may only be supplied with pipeline folder_mode")
        if raw_pipeline_step is not None:
            raise RunRequestValidationError("pipeline_step may only be supplied with pipeline folder_mode")
    if folder_mode == "explicit" and not folders:
        raise RunRequestValidationError("explicit folder_mode requires non-empty folders")
    if folder_mode != "explicit" and folders:
        raise RunRequestValidationError("folders may only be supplied with explicit folder_mode")
    if ingress_source == "api" and action not in _API_ACTIONS:
        raise RunRequestValidationError("API action must be one of plan, drift, or report")
    if ingress_source == "api" and folder_mode == "affected":
        raise RunRequestValidationError("service callers must use explicit or all folder_mode; affected requires GitHub ingress; pipeline callers must use pipeline folder_mode")
    idempotency_key = _required_str(body.get("idempotency_key"), "idempotency_key", _IDEM_KEY)
    notification_raw = body.get("notification_target")
    if notification_raw is None:
        notification = NotificationTarget("registry")
    elif not isinstance(notification_raw, dict):
        raise RunRequestValidationError("notification_target must be an object")
    else:
        _reject_unknown_keys(notification_raw, _ALLOWED_NOTIFICATION_KEYS, "notification_target")
        ntype = _required_str(notification_raw.get("type"), "notification_target.type").casefold()
        if ntype not in _NOTIFICATION_TYPES:
            raise RunRequestValidationError("notification_target.type must be github_pr or registry")
        pr_number = notification_raw.get("pr_number")
        if ntype == "github_pr":
            if ingress_source == "api":
                raise RunRequestValidationError("service callers cannot forge github_pr notification context")
            if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
                raise RunRequestValidationError("github_pr notification_target requires positive pr_number")
            notification = NotificationTarget("github_pr", pr_number=pr_number)
        else:
            if pr_number is not None:
                raise RunRequestValidationError("registry notification_target must not include pr_number")
            notification = NotificationTarget("registry")
    return RunRequest(
        trigger_id=trigger_id,
        commit_hash=commit_hash,
        action=action,
        folder_mode=folder_mode,
        folders=folders,
        pipeline=pipeline,
        pipeline_step=pipeline_step,
        idempotency_key=idempotency_key,
        notification_target=notification,
        ingress_source=ingress_source,
    )


def run_request_folder_flags(request: RunRequest) -> tuple[list[str], bool, bool]:
    """Map folder_mode to parse-command folder selection flags."""
    if request.folder_mode == "all":
        return [], True, False
    if request.folder_mode == "affected":
        return [], False, True
    if request.folder_mode == "explicit":
        return list(request.folders), False, False
    if request.folder_mode == "pipeline":
        return [], False, False
    raise RunRequestValidationError(f"unknown folder_mode: {request.folder_mode}")
