"""Shared orchestration entry for webhook and API ingress."""
from __future__ import annotations

import json
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.core.models import RepoSettings
from src.domain.run.fingerprint import request_fingerprint
from src.domain.run.request import RunRequest, run_request_folder_flags
from src.platform.aws.run_registry.keys import (
    repo_gsi_pk,
    repo_gsi_sk,
    run_meta_sk,
    run_pk,
    terminal_rank,
)
from src.platform.aws.dynamo import get_repo_settings
from src.domain.engine.outer_execution_id import compose_outer_run_id
from src.platform.aws.run_registry import (
    attach_sfn_execution_arn,
    claim_idempotent_run,
    expire_ttl,
    get_run,
    update_run_status,
)


class OrchestrationError(RuntimeError):
    """Raised when run orchestration cannot proceed."""


def _settings_dict(settings: RepoSettings) -> dict[str, Any]:
    return {
        "trigger_id": settings.trigger_id,
        "repo_name": settings.repo_name,
        "git_url": settings.git_url,
        "ssm_openci_tf_github_token": settings.ssm_openci_tf_github_token,
        "aws_default_region": settings.aws_default_region,
        "ssm_infracost_api_key": settings.ssm_infracost_api_key,
        "upstream_urls": settings.upstream_urls,
    }


def _webhook_info_from_run_request(request: RunRequest, settings: RepoSettings) -> dict[str, Any]:
    info: dict[str, Any] = {
        "event_type": "api" if request.ingress_source == "api" else request.ingress_source,
        "action": request.action,
        "repo_name": settings.repo_name,
        "commit_hash": request.commit_hash,
        "trigger_id": request.trigger_id,
        "ingress_source": request.ingress_source,
        "idempotency_key": request.idempotency_key,
        "notification_target": request.notification_target.to_dict(),
    }
    if request.notification_target.type == "github_pr" and request.notification_target.pr_number is not None:
        info["pr_number"] = request.notification_target.pr_number
    github_metadata = getattr(request, "github_metadata", None)
    if isinstance(github_metadata, dict):
        info.update(github_metadata)
    return info


def build_step_function_input(request: RunRequest, settings: RepoSettings, run_id: str) -> dict[str, Any]:
    folders, all_flag, affected_flag = run_request_folder_flags(request)
    payload: dict[str, Any] = {
        "run_request": request.to_dict(),
        "run_id": run_id,
        "webhook_info": _webhook_info_from_run_request(request, settings),
        "settings": _settings_dict(settings),
        "action": request.action,
        "folders": folders,
        "all_flag": all_flag,
        "affected_flag": affected_flag,
        "ingress_source": request.ingress_source,
        "notification_target": request.notification_target.to_dict(),
    }
    if request.pipeline is not None:
        payload["pipeline"] = request.pipeline
    if request.pipeline_step is not None:
        payload["pipeline_step"] = request.pipeline_step
    github_metadata = getattr(request, "github_metadata", None)
    if isinstance(github_metadata, dict):
        if github_metadata.get("intent_create"):
            payload["intent_create"] = True
        if github_metadata.get("intent_confirm"):
            payload["intent_confirm"] = True
        if github_metadata.get("confirm_token"):
            payload["confirm_token"] = github_metadata["confirm_token"]
    return payload


def _deterministic_execution_name(run_id: str) -> str:
    return run_id[:80]


def _reconcile_execution_arn(execution_name: str, sfn_arn: str) -> str:
    client = boto3.client("stepfunctions")
    machine_name = sfn_arn.rsplit(":", 1)[-1]
    execution_arn = f"{sfn_arn.rsplit(':', 2)[0]}:execution:{machine_name}:{execution_name}"
    history = client.describe_execution(executionArn=execution_arn)
    return str(history.get("executionArn") or execution_arn)


def _step_function_arn_for_request(request: RunRequest) -> str:
    github_metadata = getattr(request, "github_metadata", None) or {}
    if github_metadata.get("intent_confirm") and request.action == "apply":
        arn = os.environ.get("APPLY_STEP_FUNCTION_ARN", "")
        if not arn:
            raise OrchestrationError("APPLY_STEP_FUNCTION_ARN is not configured")
        return arn
    if github_metadata.get("intent_confirm") and request.action == "destroy":
        arn = os.environ.get("DESTROY_STEP_FUNCTION_ARN", "")
        if not arn:
            raise OrchestrationError("DESTROY_STEP_FUNCTION_ARN is not configured")
        return arn
    arn = os.environ.get("STEP_FUNCTION_ARN", "")
    if not arn:
        raise OrchestrationError("STEP_FUNCTION_ARN is not configured")
    return arn


def start_run_from_request(request: RunRequest) -> tuple[str, bool]:
    """Create or resume an idempotent run and start Step Functions when needed."""
    settings = get_repo_settings(request.trigger_id, with_webhook_secret=False)
    fingerprint = request_fingerprint(request)
    created_at = int(__import__("time").time())
    run_id = compose_outer_run_id(settings.repo_name, request.action)
    run_record = {
        "pk": run_pk(run_id),
        "sk": run_meta_sk(),
        "gsi1pk": repo_gsi_pk(request.trigger_id),
        "gsi1sk": repo_gsi_sk(created_at, run_id),
        "run_id": run_id,
        "trigger_id": request.trigger_id,
        "repo_name": settings.repo_name,
        "commit_hash": request.commit_hash,
        "action": request.action,
        "ingress_source": request.ingress_source,
        "notification_target": request.notification_target.to_dict(),
        "idempotency_key": request.idempotency_key,
        "request_fingerprint": fingerprint,
        "status": "accepted",
        "status_rank": 0,
        "created_at": created_at,
        "updated_at": created_at,
        "expire_ttl": expire_ttl(created_at),
    }
    if request.pipeline is not None:
        run_record["pipeline"] = request.pipeline
    if request.pipeline_step is not None:
        run_record["pipeline_step"] = request.pipeline_step
    run_id, created = claim_idempotent_run(
        request.trigger_id,
        request.idempotency_key,
        request_fingerprint=fingerprint,
        run_record=run_record,
    )
    existing = get_run(run_id)
    if existing and existing.get("sfn_execution_arn"):
        return run_id, False
    sfn_arn = _step_function_arn_for_request(request)
    payload = build_step_function_input(request, settings, run_id)
    execution_name = _deterministic_execution_name(run_id)
    client = boto3.client("stepfunctions")
    try:
        response = client.start_execution(
            stateMachineArn=sfn_arn,
            name=execution_name,
            input=json.dumps(payload),
        )
        execution_arn = str(response.get("executionArn") or "")
    except ClientError as error:
        if error.response["Error"]["Code"] != "ExecutionAlreadyExists":
            raise OrchestrationError("failed to start orchestration") from error
        execution_arn = _reconcile_execution_arn(execution_name, sfn_arn)
    try:
        attach_sfn_execution_arn(run_id, execution_arn)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise OrchestrationError("failed to attach orchestration ARN") from error
    try:
        update_run_status(run_id, "running")
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise OrchestrationError("failed to mark run running") from error
        record = get_run(run_id)
        if not record:
            raise OrchestrationError("run missing after running transition rejection") from error
        if record.get("sfn_execution_arn") != execution_arn:
            raise OrchestrationError("run already terminal with different execution ARN") from error
        if terminal_rank(str(record.get("status") or "")) < terminal_rank("running"):
            raise OrchestrationError("run already terminal before running transition") from error
    return run_id, created
