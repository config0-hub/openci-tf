# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-acceptance progress notification for run-folder submissions."""

from __future__ import annotations

import os

from botocore.exceptions import BotoCoreError, ClientError
from requests.exceptions import RequestException

from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.core.models import FolderConfig
from src.platform.aws import engine
from src.platform.aws.run_registry import record_folder_submission_notification

_MUTATION_ACTIONS = frozenset({"apply", "destroy"})
_NOTIFICATION_ERRORS = (
    BotoCoreError,
    ClientError,
    RequestException,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)


def _record_notification(
    *,
    event: dict,
    result: dict[str, object],
    status: str,
    error: str | None = None,
) -> None:
    if not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return
    record_folder_submission_notification(
        run_id=str(event["run_id"]),
        folder=str(event["folder"]),
        execution_id=str(result["exec_id"]),
        attempt=int(result["attempt"]),
        notification_status=status,
        notification_error=error,
    )


def _bounded_notification_text(value: object) -> str:
    bounded = redact_and_bound_terminal_evidence(value)
    if not isinstance(bounded, str):
        raise TypeError("notification evidence must be a string")
    return bounded


def _notification_failure(
    *, event: dict, result: dict[str, object], error: Exception
) -> dict[str, object]:
    notification_error = _bounded_notification_text(str(error))
    _record_notification(
        event=event,
        result=result,
        status="failed",
        error=notification_error,
    )
    return {
        "notification_status": "failed",
        "notification_failed": True,
        "notification_error": notification_error,
    }


def _notify_after_acceptance(
    *,
    event: dict,
    config: FolderConfig,
    lane_mode: str,
    result: dict[str, object],
) -> dict[str, object]:
    """Publish progress after durable acceptance; operational failures are evidence."""
    if lane_mode not in _MUTATION_ACTIONS:
        _record_notification(event=event, result=result, status="not_applicable")
        return {"notification_status": "not_applicable"}
    project_name = os.environ.get("ENGINE_CODEBUILD_PROJECT_NAME", "")
    if not project_name:
        _record_notification(event=event, result=result, status="skipped")
        return {
            "notification_status": "skipped",
            "notification_reason": "missing CodeBuild project name",
        }
    codebuild_build_id = result.get("codebuild_build_id")
    if not isinstance(codebuild_build_id, str) or not codebuild_build_id:
        try:
            codebuild_build_id = engine.resolve_codebuild_build_id(
                project_name, str(result["exec_id"]), max_attempts=1
            )
        except _NOTIFICATION_ERRORS as error:
            return _notification_failure(event=event, result=result, error=error)
    if not isinstance(codebuild_build_id, str) or not codebuild_build_id:
        _record_notification(event=event, result=result, status="skipped")
        return {
            "notification_status": "skipped",
            "notification_reason": "CodeBuild build id is not available yet",
        }
    from src.services.run_folder.publish_mutation_progress import (
        publish_codebuild_link,
    )

    try:
        grace_seconds = int(
            event.get("grace_seconds")
            or config.resolved_grace_seconds(str(event["action"]))
        )
    except (ValueError, TypeError, KeyError) as error:
        return _notification_failure(event=event, result=result, error=error)
    try:
        publication = publish_codebuild_link(
            run_id=str(event["run_id"]),
            repo_name=str(event["repo_name"]),
            folder=str(event["folder"]),
            action=str(event["action"]),
            commit_hash=str(event["commit_hash"]),
            grace_seconds=grace_seconds,
            outer_execution_arn=None,
            codebuild_project=project_name,
            codebuild_build_id=codebuild_build_id,
            ssm_github_token_path=str(event["ssm_openci_tf_github_token"]),
        )
    except _NOTIFICATION_ERRORS as error:
        return _notification_failure(event=event, result=result, error=error)
    result["codebuild_build_id"] = codebuild_build_id
    if publication.get("updated") is True:
        _record_notification(event=event, result=result, status="succeeded")
        return {"notification_status": "succeeded"}
    reason = _bounded_notification_text(
        publication.get("reason") or "progress notification was not applicable"
    )
    _record_notification(event=event, result=result, status="skipped")
    return {"notification_status": "skipped", "notification_reason": reason}
