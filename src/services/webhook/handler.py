# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed GitHub webhook entrypoint."""
from __future__ import annotations

import base64
import json
from typing import Any

import requests
from botocore.exceptions import BotoCoreError, ClientError

from src.core.logging import get_logger
from src.domain.authorization import can_trigger
from src.domain.command.grammar import ParseError, parse_command, unknown_verb_in_comment
from src.domain.formatters.intent import unknown_verb_refusal_comment
from src.domain.engine.invocation_id import (
    InvalidInvocationIdentityError,
    extract_delivery_id,
)
from src.platform.aws.dynamo import get_repo_settings
from src.platform.aws.ssm import get_github_token
from src.platform.github.client import get_collaborator_permission, get_pull_request
from src.services.orchestration.start_run import (
    OrchestrationError,
    start_run_from_request,
)
from src.services.webhook.parse_event import (
    extract_normalized_event,
    parse_github_event,
)
from src.services.webhook.pr_comment import post_pr_comment
from src.services.webhook.run_request import github_run_request
from src.services.webhook.validate import verify_signature

logger = get_logger(__name__)


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _command_from_info(info: Any) -> tuple[str, list[str], bool, bool, str | None, int | None] | None:
    if info.event_type == "pull_request":
        return "plan", [], False, True, None, None
    comment_body = info.comment_body or ""
    if not comment_body:
        return None
    try:
        cmd = parse_command(comment_body)
    except ParseError:
        return None
    return cmd.effective_action, list(cmd.folders), bool(cmd.all_flag), bool(cmd.affected_flag), cmd.pipeline, cmd.pipeline_step


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    norm = extract_normalized_event(event)
    if not norm.trigger_id:
        return _response(400, {"error": "Missing trigger_id"})
    body = base64.b64decode(norm.body) if norm.is_base64 else (norm.body.encode() if isinstance(norm.body, str) else norm.body)
    try:
        settings = get_repo_settings(norm.trigger_id)
    except ValueError:
        return _response(404, {"error": "Unknown trigger_id"})
    if norm.source == "api_gateway" and not verify_signature(body, norm.headers.get("x-hub-signature-256", ""), settings.secret):
        return _response(401, {"error": "Invalid signature"})
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return _response(400, {"error": "Invalid JSON"})
    info = parse_github_event(norm.headers.get("x-github-event", ""), payload, norm.trigger_id)
    if not info:
        return _response(200, {"message": "Event ignored"})
    if info.repo_name.casefold() != settings.repo_name.casefold():
        return _response(403, {"error": "Repository mismatch"})
    try:
        token = get_github_token(settings.ssm_openci_tf_github_token)
        if info.pr_api_url and (not info.commit_hash or not info.head_repo_name):
            pr = get_pull_request(info.pr_api_url, token)
            info.commit_hash = pr.get("head", {}).get("sha")
            info.head_repo_name = pr.get("head", {}).get("repo", {}).get("full_name")
            info.base_repo_name = pr.get("base", {}).get("repo", {}).get("full_name")
    except (requests.RequestException, BotoCoreError, ClientError):
        return _response(502, {"error": "Unable to pin pull request head"})
    if info.pr_number and (not info.commit_hash or not info.head_repo_name or not info.base_repo_name):
        return _response(422, {"error": "Missing pull request head"})
    head_repo = info.head_repo_name
    base_repo = info.base_repo_name
    if info.pr_number and head_repo is not None and base_repo is not None and head_repo.casefold() != base_repo.casefold():
        return _response(403, {"error": "Fork pull requests are refused"})
    try:
        permission = get_collaborator_permission(settings.repo_name, info.username, token)
    except requests.RequestException:
        return _response(403, {"error": "Actor permission denied"})
    if not can_trigger(permission):
        return _response(403, {"error": "Actor permission denied"})
    try:
        delivery_id = extract_delivery_id(norm.headers)
    except InvalidInvocationIdentityError as error:
        return _response(400, {"error": str(error)})
    if delivery_id is None:
        return _response(400, {"error": "missing GitHub delivery id"})
    parsed = _command_from_info(info)
    if parsed is None:
        if info.event_type == "issue_comment":
            unknown_verb = unknown_verb_in_comment(info.comment_body or "")
            if unknown_verb is not None and info.pr_number is not None:
                post_pr_comment(
                    {"pr_number": info.pr_number, "repo_name": info.repo_name},
                    {"ssm_openci_tf_github_token": settings.ssm_openci_tf_github_token},
                    unknown_verb_refusal_comment(unknown_verb),
                )
        return _response(200, {"message": "Event ignored"})
    action, folders, all_flag, affected_flag, pipeline, pipeline_step = parsed
    if action in {"apply", "destroy"}:
        try:
            cmd = parse_command(info.comment_body or "")
        except ParseError:
            return _response(200, {"message": "Event ignored"})
        request = github_run_request(
            {
                "trigger_id": info.trigger_id,
                "commit_hash": info.commit_hash,
                "pr_number": info.pr_number,
                "comment_id": info.comment_id,
                "event_type": info.event_type,
                "username": info.username,
            },
            action=action,
            folders=list(cmd.folders),
            all_flag=False,
            affected_flag=False,
            delivery_id=delivery_id,
            confirm_token=cmd.confirm_token,
            intent_create=cmd.confirm_token is None,
            intent_confirm=cmd.confirm_token is not None,
            pipeline=cmd.pipeline,
            pipeline_step=cmd.pipeline_step,
        )
        try:
            run_id, created = start_run_from_request(request)
        except OrchestrationError:
            return _response(502, {"error": "Unable to start run"})
        return _response(200, {"message": "Accepted", "run_id": run_id, "created": created})
    if action not in {"plan", "drift", "report", "plan_destroy"}:
        return _response(200, {"message": "Unsafe action ignored"})
    request = github_run_request(
        {
            "trigger_id": info.trigger_id,
            "commit_hash": info.commit_hash,
            "pr_number": info.pr_number,
            "comment_id": info.comment_id,
            "event_type": info.event_type,
            "username": info.username,
        },
        action=action,
        folders=folders,
        all_flag=all_flag,
        affected_flag=affected_flag,
        delivery_id=delivery_id,
        pipeline=pipeline,
        pipeline_step=pipeline_step,
    )
    try:
        run_id, created = start_run_from_request(request)
    except OrchestrationError:
        return _response(502, {"error": "Unable to start run"})
    return _response(200, {"message": "Accepted", "run_id": run_id, "created": created})
