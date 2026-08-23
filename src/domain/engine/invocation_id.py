"""Deterministic per-webhook invocation identifiers."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_GUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SAFE_CHARS = re.compile(r"^[\w+=,.@-]+$")
_MAX_RUN_ID_BODY = 48
_STS_SESSION_MIN = 2
_STS_SESSION_MAX = 64
_MAX_BOUNDED_INT = 999_999_999_999_999_999_999


class InvalidInvocationIdentityError(ValueError):
    """Raised when webhook delivery or comment metadata is malformed."""


def _normalize_guid(value: str, *, field: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidInvocationIdentityError(f"{field} must not be empty")
    if not _GUID.fullmatch(text):
        raise InvalidInvocationIdentityError(f"{field} must be a UUID-shaped token")
    return text.lower()


def _positive_bounded_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidInvocationIdentityError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise InvalidInvocationIdentityError(f"{field} must be a positive integer") from error
    if number < 1 or number > _MAX_BOUNDED_INT:
        raise InvalidInvocationIdentityError(f"{field} must be a positive integer up to {_MAX_BOUNDED_INT}")
    return number


def _hash_token(value: str) -> str:
    """Hash a normalized ingress token before embedding it in run_id."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def extract_delivery_id(headers: dict[str, str]) -> str | None:
    """Read X-GitHub-Delivery case-insensitively and validate UUID shape."""
    raw = None
    for key, value in headers.items():
        if key.lower() == "x-github-delivery":
            raw = value
            break
    if raw is None:
        return None
    return _normalize_guid(raw, field="delivery_id")


def extract_request_id(event: dict[str, Any]) -> str | None:
    """Read API Gateway requestContext.requestId when present."""
    request_context = event.get("requestContext")
    if not isinstance(request_context, dict):
        return None
    raw = request_context.get("requestId")
    if raw is None:
        return None
    return _normalize_guid(str(raw), field="request_id")


def validate_comment_id(value: Any) -> str:
    return str(_positive_bounded_int(value, field="comment_id"))


def validate_delivery_id(value: Any) -> str:
    return _normalize_guid(str(value), field="delivery_id")


def _sanitize_part(value: str, *, field: str) -> str:
    safe = re.sub(r"[^\w+=,.@-]", "-", value.strip())
    if not safe:
        raise InvalidInvocationIdentityError(f"{field} is required")
    return safe


def _fallback_delivery_id(webhook: dict[str, Any]) -> str:
    """Deterministic content hash for direct/internal fixtures lacking ingress IDs."""
    material = "|".join(
        (
            str(webhook.get("event_type", "")),
            str(webhook.get("action", "")),
            str(webhook.get("repo_name", "")),
            str(webhook.get("pr_number") or webhook.get("issue_number") or ""),
            str(webhook.get("comment_body") or ""),
            str(webhook.get("commit_hash") or ""),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def _resolve_ingress_token(webhook: dict[str, Any]) -> str:
    delivery_id = webhook.get("delivery_id")
    if delivery_id is not None:
        return _hash_token(validate_delivery_id(delivery_id))
    ingress_request_id = webhook.get("ingress_request_id")
    if ingress_request_id is not None:
        return _hash_token(_normalize_guid(str(ingress_request_id), field="request_id"))
    return _fallback_delivery_id(webhook)


def _compact_run_id(raw: str) -> str:
    safe = re.sub(r"[^\w+=,.@-]", "-", raw)
    if len(safe) <= _MAX_RUN_ID_BODY and _SAFE_CHARS.fullmatch(safe):
        return safe
    digest = hashlib.sha256(safe.encode()).hexdigest()[:24]
    prefix = safe[:16]
    return f"{prefix}-{digest}"


def derive_run_id(webhook: dict[str, Any]) -> str:
    """Derive a deterministic run_id for one webhook invocation."""
    trigger_id = _sanitize_part(str(webhook.get("trigger_id", "")), field="trigger_id")
    event_type = webhook.get("event_type", "")

    if event_type == "issue_comment":
        pr_number = webhook.get("pr_number")
        if pr_number is None:
            raise InvalidInvocationIdentityError("pr_number required for issue_comment run_id")
        comment_id = webhook.get("comment_id")
        if comment_id is None:
            raise InvalidInvocationIdentityError("comment_id required for issue_comment run_id")
        raw = f"{trigger_id}-{_positive_bounded_int(pr_number, field='pr_number')}-c{validate_comment_id(comment_id)}"
    elif event_type == "pull_request":
        pr_number = webhook.get("pr_number")
        if pr_number is None:
            raise InvalidInvocationIdentityError("pr_number required for pull_request run_id")
        ingress_token = _resolve_ingress_token(webhook)
        raw = f"{trigger_id}-{_positive_bounded_int(pr_number, field='pr_number')}-d{ingress_token}"
    elif event_type == "issues":
        issue_number = webhook.get("issue_number")
        if issue_number is None:
            raise InvalidInvocationIdentityError("issue_number required for issues run_id")
        ingress_token = _resolve_ingress_token(webhook)
        raw = f"{trigger_id}-i{_positive_bounded_int(issue_number, field='issue_number')}-d{ingress_token}"
    elif event_type == "api":
        idempotency_key = webhook.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise InvalidInvocationIdentityError("idempotency_key required for api run_id")
        raw = f"{trigger_id}-api{_hash_token(idempotency_key.strip())}"
    else:
        digest = hashlib.sha256(json.dumps(webhook, sort_keys=True, default=str).encode()).hexdigest()[:16]
        raw = f"{trigger_id}-f{digest}"

    run_id = _compact_run_id(raw)
    assert_execution_id_bounds(f"{run_id}.{'0' * 12}.0")
    return run_id


def assert_execution_id_bounds(execution_id: str) -> None:
    if not (_STS_SESSION_MIN <= len(execution_id) <= _STS_SESSION_MAX):
        raise ValueError(f"execution_id length {len(execution_id)} outside STS bounds 2-64")
    if not _SAFE_CHARS.fullmatch(execution_id):
        raise ValueError("execution_id contains disallowed characters")
