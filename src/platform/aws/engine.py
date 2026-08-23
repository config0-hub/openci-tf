"""Engine init-job and CodeBuild lane submission contracts."""
from __future__ import annotations

import json
import os
import time
import uuid

import boto3

from src.core.aws_ids import is_valid_codebuild_build_id
from src.core.errors import EngineAckError


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


def start_codebuild_execution(state_machine_arn: str, payload: dict) -> dict:
    """Start the engine CodeBuild state machine directly (mutation lanes only)."""
    trigger_id = str(payload.get("trigger_id") or "")
    if not trigger_id:
        raise EngineAckError("codebuild submission requires trigger_id")
    execution_name = trigger_id[:80] if trigger_id else uuid.uuid4().hex[:80]
    response = boto3.client("stepfunctions").start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(payload),
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
