# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalize GitHub webhook payloads into WebhookInfo.

Also provides event-source detection for multi-source Lambda invocation
(API Gateway, SNS, direct invocation).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from src.core.models import WebhookInfo
from src.domain.engine.invocation_id import InvalidInvocationIdentityError, extract_request_id


# ---------------------------------------------------------------------------
# Event source detection
# ---------------------------------------------------------------------------

@dataclass
class NormalizedEvent:
    """Result of event source detection and unwrapping."""

    source: str  # "api_gateway", "sns", or "direct"
    body: str | bytes  # raw body (string or bytes)
    headers: dict[str, str]  # HTTP headers (lowercased keys)
    trigger_id: str  # extracted from path or payload
    is_base64: bool = False
    request_id: str | None = None  # API Gateway requestContext.requestId when present


def detect_event_source(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Detect the Lambda invocation source and return (source, payload).

    Returns:
        ("api_gateway", event)  — API Gateway proxy event
        ("sns", message_dict)   — unwrapped SNS message
        ("direct", event)       — direct Lambda invocation
    """
    if "Records" in event:
        records = event["Records"]
        if records and "Sns" in records[0]:
            message = records[0]["Sns"]["Message"]
            if isinstance(message, str):
                message = json.loads(message)
            return "sns", message

    if "httpMethod" in event or "requestContext" in event:
        return "api_gateway", event

    return "direct", event


def extract_normalized_event(event: dict[str, Any]) -> NormalizedEvent:
    """Extract a NormalizedEvent from any supported Lambda event source.

    API Gateway:
        body from event["body"], headers from event["headers"],
        trigger_id from pathParameters.

    SNS / Direct:
        Unwrapped payload must contain "body", "headers", and "trigger_id"
        at the top level — i.e. the same shape as an API Gateway event or
        a dict with those keys explicitly set.
    """
    source, payload = detect_event_source(event)

    request_id = None
    if source == "api_gateway":
        trigger_id = (payload.get("pathParameters") or {}).get("trigger_id", "")
        raw_headers = payload.get("headers") or {}
        body = payload.get("body", "")
        is_base64 = payload.get("isBase64Encoded", False)
        try:
            request_id = extract_request_id(payload)
        except InvalidInvocationIdentityError:
            # request_id (requestContext.requestId) is an OPTIONAL API Gateway
            # correlation field used only as an ingress-identity fallback; a
            # malformed value is treated the same as an absent one. This
            # deliberately diverges from handler.py's hard-400 policy for
            # extract_delivery_id, which validates the REQUIRED
            # X-GitHub-Delivery header that anchors run identity/idempotency.
            request_id = None
    else:
        # SNS and direct invocation: payload is the unwrapped message.
        # Support two shapes:
        #   1. API-Gateway-like: has "body", "headers", "pathParameters"
        #   2. Explicit: has "body", "headers", "trigger_id" at top level
        trigger_id = (
            payload.get("trigger_id")
            or (payload.get("pathParameters") or {}).get("trigger_id", "")
        )
        raw_headers = payload.get("headers") or {}
        body = payload.get("body", "")
        is_base64 = payload.get("isBase64Encoded", False)

    headers = {k.lower(): v for k, v in raw_headers.items()}

    return NormalizedEvent(
        source=source,
        body=body,
        headers=headers,
        trigger_id=trigger_id,
        is_base64=is_base64,
        request_id=request_id,
    )

# Actions that indicate an actionable PR comment
ACTIONABLE_COMMENT_ACTIONS = {"created"}

# Actions that indicate an actionable PR event
ACTIONABLE_PR_ACTIONS = {"opened", "synchronize"}

# Actions that indicate an actionable issue event
ACTIONABLE_ISSUE_ACTIONS = {"opened"}

# Valid first word in a comment that indicates an openci-tf command
COMMAND_PREFIXES = {"tf"}


def parse_github_event(
    event_type: str, payload: dict[str, Any], trigger_id: str
) -> Optional[WebhookInfo]:
    """Parse a GitHub webhook payload into WebhookInfo.

    Returns None if the event is not actionable (e.g. wrong event type,
    non-command comment, etc.).

    Args:
        event_type: The X-GitHub-Event header value.
        payload: The parsed JSON payload body.
        trigger_id: The trigger_id from the API Gateway path.
    """
    if event_type == "ping":
        return None

    if event_type == "issue_comment":
        return _parse_issue_comment(payload, trigger_id)

    if event_type == "pull_request":
        return _parse_pull_request(payload, trigger_id)

    if event_type == "issues":
        return _parse_issue(payload, trigger_id)

    return None


def _parse_issue_comment(
    payload: dict[str, Any], trigger_id: str
) -> Optional[WebhookInfo]:
    """Parse issue_comment event — this is the primary command trigger."""
    action = payload.get("action", "")
    if action not in ACTIONABLE_COMMENT_ACTIONS:
        return None

    raw_comment_body = payload.get("comment", {}).get("body", "")
    comment_body = raw_comment_body if isinstance(raw_comment_body, str) else ""
    if not comment_body.strip():
        return None

    # Check if first word looks like an openci-tf command. Keep the original
    # body for parse_command so the single-line grammar sees line boundaries.
    first_word = comment_body.strip().split()[0].lower()
    if first_word not in COMMAND_PREFIXES:
        return None

    issue = payload.get("issue", {})
    # issue_comment fires for both issues and PRs — check for pull_request key
    pr_number = None
    issue_number = None
    if "pull_request" in issue:
        pr_number = issue["number"]
    else:
        issue_number = issue["number"]

    repo = payload.get("repository", {})

    # For PR comments, extract the pull_request API URL for commit_hash lookup
    pr_api_url = None
    if pr_number and "pull_request" in issue:
        pr_api_url = issue["pull_request"].get("url")

    return WebhookInfo(
        event_type="issue_comment",
        action=action,
        repo_name=repo.get("full_name", ""),
        pr_number=pr_number,
        issue_number=issue_number,
        comment_body=comment_body,
        username=payload.get("comment", {}).get("user", {}).get("login", ""),
        trigger_id=trigger_id,
        pr_api_url=pr_api_url,
        comment_id=payload.get("comment", {}).get("id"),
    )


def _parse_pull_request(
    payload: dict[str, Any], trigger_id: str
) -> Optional[WebhookInfo]:
    """Parse pull_request event for signature and head pinning only."""
    action = payload.get("action", "")
    if action not in ACTIONABLE_PR_ACTIONS:
        return None

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    return WebhookInfo(
        event_type="pull_request",
        action=action,
        repo_name=repo.get("full_name", ""),
        pr_number=pr.get("number"),
        username=pr.get("user", {}).get("login", ""),
        commit_hash=pr.get("head", {}).get("sha"),
        trigger_id=trigger_id,
        head_repo_name=pr.get("head", {}).get("repo", {}).get("full_name"),
        base_repo_name=pr.get("base", {}).get("repo", {}).get("full_name"),
    )


def _parse_issue(
    payload: dict[str, Any], trigger_id: str
) -> Optional[WebhookInfo]:
    """Parse issues event — for issue-driven reports."""
    action = payload.get("action", "")
    if action not in ACTIONABLE_ISSUE_ACTIONS:
        return None

    issue = payload.get("issue", {})
    raw_body = issue.get("body", "")
    body = raw_body if isinstance(raw_body, str) else ""
    if not body.strip():
        return None

    # Check if issue body starts with an openci-tf command. Keep the original
    # body for parse_command so the single-line grammar sees line boundaries.
    first_word = body.strip().split()[0].lower()
    if first_word not in COMMAND_PREFIXES:
        return None

    repo = payload.get("repository", {})
    return WebhookInfo(
        event_type="issues",
        action=action,
        repo_name=repo.get("full_name", ""),
        issue_number=issue.get("number"),
        comment_body=body,
        username=issue.get("user", {}).get("login", ""),
        trigger_id=trigger_id,
    )
