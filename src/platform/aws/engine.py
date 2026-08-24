# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine init-job and CodeBuild lane submission contracts."""
from __future__ import annotations

import json
import math
import os
import time
import uuid

import boto3

from src.core.aws_ids import is_valid_codebuild_build_id
from src.core.errors import EngineAckError

# Contract source: aws-execution-engine/aws_exe_sys/init_job/dispatcher.py at 78c2fcd
_CODEBUILD_TIMEOUT_MARGIN_MINUTES = 3
_SFN_TIMEOUT_MARGIN_SECONDS = 300 + 300
_CODEBUILD_PAYLOAD_FIELDS = (
    "trigger_id",
    "s3_package_uri",
    "sops_type",
    "sops_path",
    "commands_b64",
    "done_endpoint",
    "execution_target",
    "timeout_seconds",
    "callback_url",
    "callback_token",
    "execution_mode",
)


def invoke_init_job(function_name: str, payload: dict) -> dict:
    response = boto3.client("lambda").invoke(FunctionName=function_name, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
    raw = response["Payload"].read()
    try:
        ack = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise EngineAckError("malformed engine acknowledgement") from error
    if not isinstance(ack, dict) or ack.get("status") != "ok" or not ack.get("trigger_id"):
        raise EngineAckError("engine acknowledgement missing status or trigger_id")
    if ack["trigger_id"] != payload.get("trigger_id"):
        raise EngineAckError("engine acknowledgement trigger_id mismatch")
    return ack


def resolve_codebuild_build_id(
    project_name: str,
    trigger_id: str,
    *,
    max_attempts: int = 60,
    sleep_seconds: float = 1.0,
    max_pages: int = 5,
    page_size: int = 10,
) -> str | None:
    """Locate the real CodeBuild build ID for one engine submission via TRIGGER_ID."""
    if not project_name or not trigger_id:
        return None
    client = boto3.client("codebuild")
    for _ in range(max_attempts):
        next_token: str | None = None
        for _ in range(max_pages):
            request: dict[str, object] = {
                "projectName": project_name,
                "sortOrder": "DESCENDING",
            }
            if next_token:
                request["nextToken"] = next_token
            response = client.list_builds_for_project(**request)
            build_ids = response.get("ids") if isinstance(response.get("ids"), list) else []
            if build_ids:
                details = client.batch_get_builds(ids=build_ids[:page_size]).get("builds", [])
                for build in details:
                    if not isinstance(build, dict):
                        continue
                    build_id = build.get("id")
                    if not isinstance(build_id, str) or not is_valid_codebuild_build_id(build_id):
                        continue
                    env = build.get("environment")
                    variables = env.get("environmentVariables") if isinstance(env, dict) else None
                    if not isinstance(variables, list):
                        continue
                    for item in variables:
                        if not isinstance(item, dict):
                            continue
                        if item.get("name") == "TRIGGER_ID" and item.get("value") == trigger_id:
                            return build_id
            next_token = response.get("nextToken")
            if not isinstance(next_token, str) or not next_token:
                break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return None


def _parse_positive_timeout_seconds(raw: object) -> int:
    if isinstance(raw, bool):
        raise EngineAckError("codebuild submission requires positive int timeout_seconds")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isdigit():
        value = int(raw)
    else:
        raise EngineAckError("codebuild submission requires positive int timeout_seconds")
    if value <= 0:
        raise EngineAckError("codebuild submission requires positive int timeout_seconds")
    return value


def _derive_codebuild_timeout_fields(timeout_seconds: object) -> tuple[int, int]:
    timeout = _parse_positive_timeout_seconds(timeout_seconds)
    build_timeout_minutes = math.ceil(timeout / 60) + _CODEBUILD_TIMEOUT_MARGIN_MINUTES
    sfn_timeout_seconds = timeout + _SFN_TIMEOUT_MARGIN_SECONDS
    return build_timeout_minutes, sfn_timeout_seconds


def _codebuild_sfn_input(payload: dict) -> dict[str, object]:
    """Match engine dispatcher transport: payload fields as strings, derived timeouts numeric."""
    build_timeout_minutes, sfn_timeout_seconds = _derive_codebuild_timeout_fields(
        payload.get("timeout_seconds")
    )
    sfn_input: dict[str, object] = {
        field: str(payload.get(field) or "") for field in _CODEBUILD_PAYLOAD_FIELDS
    }
    sfn_input["build_timeout_minutes"] = build_timeout_minutes
    sfn_input["sfn_timeout_seconds"] = sfn_timeout_seconds
    return sfn_input


def start_codebuild_execution(state_machine_arn: str, payload: dict) -> dict:
    """Start the engine CodeBuild state machine directly (mutation lanes only)."""
    trigger_id = str(payload.get("trigger_id") or "")
    if not trigger_id:
        raise EngineAckError("codebuild submission requires trigger_id")
    sfn_input = _codebuild_sfn_input(payload)
    execution_name = trigger_id[:80] if trigger_id else uuid.uuid4().hex[:80]
    response = boto3.client("stepfunctions").start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(sfn_input),
    )
    execution_arn = response.get("executionArn")
    if not execution_arn:
        raise EngineAckError("codebuild state machine start missing executionArn")
    project_name = os.environ.get("ENGINE_CODEBUILD_PROJECT_NAME", "")
    build_id = resolve_codebuild_build_id(project_name, trigger_id) if project_name else None
    result: dict[str, str] = {
        "status": "ok",
        "trigger_id": trigger_id,
        "engine_execution_arn": execution_arn,
    }
    if build_id:
        result["codebuild_build_id"] = build_id
    return result
