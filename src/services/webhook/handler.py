# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed GitHub webhook entrypoint."""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import requests
from botocore.exceptions import BotoCoreError, ClientError

from src.core.logging import get_logger
from src.core.models import Command
from src.domain.authorization import can_trigger
from src.domain.command.grammar import ParseError, parse_command
from src.domain.engine.invocation_id import (
    InvalidInvocationIdentityError,
    extract_delivery_id,
)
from src.domain.formatters.artifacts import (
    _redact_confirm_token,
    closed_pr_rejection_comment,
)
from src.domain.formatters.command_audit import unsupported_command_help_comment
from src.platform.aws.dynamo import get_repo_settings
from src.platform.aws.ssm import get_github_token
from src.platform.github.client import (
    GitHubClient,
    comment_url,
    get_collaborator_permission,
    get_pull_request,
)
from src.platform.github.command_audit import record_command_audit
from src.platform.github.command_comment_cleanup import (
    defer_command_comment_cleanup,
    delete_acknowledged_command_comment,
)
from src.services.orchestration.start_run import (
    OrchestrationError,
    start_run_from_request,
)
from src.services.webhook.parse_event import (
    extract_normalized_event,
    parse_github_event,
)
from src.services.webhook.run_request import github_run_request
from src.services.webhook.validate import verify_signature

logger = get_logger(__name__)

_UNSUPPORTED_REJECTION_SLEEP_SECONDS = 10


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _has_actionable_command(info: Any) -> bool:
    return bool((info.comment_body or "").strip())


def _parse_issue_comment(info: Any) -> Command:
    return parse_command(info.comment_body or "")


def _starts_with_tf_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    first = stripped.split()[0]
    return first.lower() == "tf"


def _record_audit_and_maybe_delete_command(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    comment_id: int | None,
    comment_body: str,
    status: str,
    *,
    delete_comment: bool,
) -> None:
    record_command_audit(
        client,
        repo,
        pr_number,
        command_text=comment_body,
        status=status,
    )
    if delete_comment:
        delete_acknowledged_command_comment(client, repo, comment_id)


def _handle_unsupported_tf_command(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    comment_id: int | None,
    comment_body: str,
) -> None:
    record_command_audit(
        client,
        repo,
        pr_number,
        command_text=comment_body,
        status="not supported",
    )
    rejection_id = client.create_comment(repo, pr_number, unsupported_command_help_comment())
    time.sleep(_UNSUPPORTED_REJECTION_SLEEP_SECONDS)
    delete_acknowledged_command_comment(client, repo, rejection_id)
    delete_acknowledged_command_comment(client, repo, comment_id)


def _post_command_rejection_and_cleanup(
    *,
    client: GitHubClient,
    repo: str,
    pr_number: int,
    comment_id: int | None,
    body: str,
) -> None:
    client.create_comment(repo, pr_number, body)
    delete_acknowledged_command_comment(client, repo, comment_id)


def _github_info_dict(info: Any) -> dict[str, Any]:
    comment_body = info.comment_body
    if isinstance(comment_body, str):
        comment_body = _redact_confirm_token(comment_body)
    return {
        "trigger_id": info.trigger_id,
        "commit_hash": info.commit_hash,
        "pr_number": info.pr_number,
        "comment_id": info.comment_id,
        "event_type": info.event_type,
        "username": info.username,
        "comment_body": comment_body,
    }


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
    if info.event_type == "pull_request":
        return _response(200, {"message": "Event ignored", "reason": "pull_request_event"})
    pr_state: str | None = None
    try:
        token = get_github_token(settings.ssm_openci_tf_github_token)
        if info.pr_api_url:
            pr = get_pull_request(info.pr_api_url, token)
            raw_state = pr.get("state")
            pr_state = str(raw_state) if raw_state else None
            if not info.commit_hash or not info.head_repo_name:
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
    if not _has_actionable_command(info):
        return _response(200, {"message": "Event ignored"})
    if info.event_type == "issue_comment" and info.pr_number and pr_state != "open":
        comment_link = (
            comment_url(settings.repo_name, info.pr_number, info.comment_id)
            if info.comment_id is not None
            else None
        )
        rejection_body = closed_pr_rejection_comment(
            comment_id=info.comment_id,
            comment_link=comment_link,
            comment_body=info.comment_body,
        )
        try:
            client = GitHubClient(token)
            record_command_audit(
                client,
                settings.repo_name,
                info.pr_number,
                command_text=info.comment_body or "",
                status="not supported",
            )
            _post_command_rejection_and_cleanup(
                client=client,
                repo=settings.repo_name,
                pr_number=info.pr_number,
                comment_id=info.comment_id,
                body=rejection_body,
            )
        except requests.RequestException:
            logger.warning(
                "failed to post closed-PR rejection comment for pr %s",
                info.pr_number,
            )
        return _response(
            200,
            {"message": "Event ignored", "reason": "pull_request_not_open"},
        )
    try:
        cmd = _parse_issue_comment(info)
    except ParseError:
        if info.pr_number and _starts_with_tf_command(info.comment_body or ""):
            try:
                client = GitHubClient(token)
                _handle_unsupported_tf_command(
                    client=client,
                    repo=settings.repo_name,
                    pr_number=info.pr_number,
                    comment_id=info.comment_id,
                    comment_body=info.comment_body or "",
                )
            except requests.RequestException:
                logger.warning(
                    "failed to handle unsupported tf command for pr %s",
                    info.pr_number,
                )
        return _response(
            200,
            {"message": "Event ignored", "reason": "invalid_command"},
        )
    if info.pr_number:
        try:
            client = GitHubClient(token)
            _record_audit_and_maybe_delete_command(
                client,
                settings.repo_name,
                info.pr_number,
                info.comment_id,
                info.comment_body or "",
                "accepted",
                delete_comment=not defer_command_comment_cleanup(cmd.action),
            )
        except requests.RequestException:
            logger.warning(
                "failed to record command audit for pr %s",
                info.pr_number,
            )
    action = cmd.effective_action
    folders = list(cmd.folders)
    all_flag = bool(cmd.all_flag)
    affected_flag = bool(cmd.affected_flag)
    if action in {"apply", "destroy"}:
        request = github_run_request(
            _github_info_dict(info),
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
    if action not in {"plan", "report", "plan_destroy"}:
        return _response(200, {"message": "Unsafe action ignored"})
    request = github_run_request(
        _github_info_dict(info),
        action=action,
        folders=folders,
        all_flag=all_flag,
        affected_flag=affected_flag,
        delivery_id=delivery_id,
        pipeline=cmd.pipeline,
        pipeline_step=cmd.pipeline_step,
    )
    try:
        run_id, created = start_run_from_request(request)
    except OrchestrationError:
        return _response(502, {"error": "Unable to start run"})
    return _response(200, {"message": "Accepted", "run_id": run_id, "created": created})
