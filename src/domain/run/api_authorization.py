# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side API caller authorization keyed by verified IAM principal."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


class ApiAuthorizationError(PermissionError):
    """Raised when a verified caller is not allowed to perform an API action."""


@dataclass(frozen=True)
class CallerPolicy:
    trigger_ids: frozenset[str]
    actions: frozenset[str]
    artifact_classes: frozenset[str]
    binary_plan: bool
    read_classes: frozenset[str]


_ALLOWED_POLICY_KEYS = frozenset(
    {"trigger_ids", "actions", "artifact_classes", "binary_plan", "read_classes"}
)
_ALLOWED_READ_CLASSES = frozenset({"admin"})
_ALLOWED_ACTIONS = frozenset({"plan", "drift", "report"})
_STS_ASSUMED_ROLE = re.compile(
    r"^arn:aws:sts::(?P<account>\d{12}):assumed-role/(?P<role>[^/]+)/.+$"
)
_IAM_ROLE = re.compile(r"^arn:aws:iam::(?P<account>\d{12}):role/(?P<role>.+)$")


def _canonical_role_arn(arn: str) -> str:
    match = _STS_ASSUMED_ROLE.match(arn)
    if match:
        return f"arn:aws:iam::{match.group('account')}:role/{match.group('role')}"
    match = _IAM_ROLE.match(arn)
    if match:
        return arn
    return arn


def _parse_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ApiAuthorizationError("binary_plan must be a boolean")
    return value


def _string_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ApiAuthorizationError(f"{label} must be a non-empty list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ApiAuthorizationError(f"{label} must contain only non-empty strings")
        items.append(item)
    if not items:
        raise ApiAuthorizationError(f"{label} must contain at least one value")
    return items


def _load_policies() -> list[tuple[re.Pattern[str], CallerPolicy]]:
    raw = os.environ.get("API_CALLER_POLICY_JSON", "{}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ApiAuthorizationError("API caller policy is misconfigured") from error
    if not isinstance(payload, dict):
        raise ApiAuthorizationError("API caller policy must be an object")
    policies: list[tuple[re.Pattern[str], CallerPolicy]] = []
    for principal_pattern, entry in payload.items():
        if not isinstance(principal_pattern, str) or not principal_pattern:
            raise ApiAuthorizationError("API caller policy principal pattern must be a non-empty string")
        if not isinstance(entry, dict):
            raise ApiAuthorizationError(f"API caller policy for {principal_pattern!r} must be an object")
        unknown = sorted(set(entry) - _ALLOWED_POLICY_KEYS)
        if unknown:
            raise ApiAuthorizationError(f"unknown API caller policy fields: {', '.join(unknown)}")
        trigger_ids = frozenset(_string_list(entry.get("trigger_ids"), label="trigger_ids"))
        actions = frozenset(item.casefold() for item in _string_list(entry.get("actions"), label="actions"))
        unknown_actions = sorted(actions - _ALLOWED_ACTIONS)
        if unknown_actions:
            raise ApiAuthorizationError(
                f"unsupported API caller policy actions: {', '.join(unknown_actions)}"
            )
        artifact_classes = frozenset(_string_list(entry.get("artifact_classes"), label="artifact_classes"))
        if "binary_plan" not in entry:
            raise ApiAuthorizationError("binary_plan must be explicitly set")
        binary_plan = _parse_bool(entry.get("binary_plan"))
        raw_read_classes = entry.get("read_classes", [])
        if not isinstance(raw_read_classes, list):
            raise ApiAuthorizationError("read_classes must be a list")
        read_classes = frozenset(
            item.casefold()
            for item in raw_read_classes
            if isinstance(item, str) and item
        )
        if len(read_classes) != len(raw_read_classes):
            raise ApiAuthorizationError("read_classes must contain unique non-empty strings")
        unknown_read_classes = sorted(read_classes - _ALLOWED_READ_CLASSES)
        if unknown_read_classes:
            raise ApiAuthorizationError(
                f"unknown API read classes: {', '.join(unknown_read_classes)}"
            )
        policies.append(
            (
                re.compile(f"^{re.escape(_canonical_role_arn(principal_pattern))}$"),
                CallerPolicy(
                    trigger_ids,
                    actions,
                    artifact_classes,
                    binary_plan,
                    read_classes,
                ),
            )
        )
    return policies


def _caller_arn(event: dict[str, Any]) -> str:
    ctx = event.get("requestContext") or {}
    identity = ctx.get("authorizer") or {}
    arn = identity.get("iam", {}).get("userArn") or identity.get("userArn")
    if isinstance(arn, str) and arn:
        return _canonical_role_arn(arn)
    raise ApiAuthorizationError("missing verified caller identity")


def resolve_caller_policy(event: dict[str, Any]) -> CallerPolicy:
    arn = _caller_arn(event)
    for pattern, policy in _load_policies():
        if pattern.match(arn):
            return policy
    raise ApiAuthorizationError(f"caller {arn} is not authorized for the API")


def authorize_create_run(event: dict[str, Any], *, trigger_id: str, action: str) -> CallerPolicy:
    policy = resolve_caller_policy(event)
    if trigger_id not in policy.trigger_ids:
        raise ApiAuthorizationError("caller is not authorized for this repository")
    if action.casefold() not in policy.actions:
        raise ApiAuthorizationError("caller is not authorized for this action")
    return policy


def authorize_read_run(event: dict[str, Any], *, trigger_id: str, action: str | None = None) -> CallerPolicy:
    policy = resolve_caller_policy(event)
    if trigger_id not in policy.trigger_ids:
        raise ApiAuthorizationError("caller is not authorized for this repository")
    if action is not None and action.casefold() not in policy.actions:
        raise ApiAuthorizationError("caller is not authorized for this action")
    return policy


def authorize_list_runs(
    event: dict[str, Any],
    *,
    trigger_id: str | None = None,
) -> tuple[CallerPolicy, tuple[str, ...]]:
    """Authorize one repository partition or the caller's complete partition set."""
    policy = resolve_caller_policy(event)
    if trigger_id is not None:
        if trigger_id not in policy.trigger_ids:
            raise ApiAuthorizationError("caller is not authorized for this repository")
        return policy, (trigger_id,)
    return policy, tuple(sorted(policy.trigger_ids))


def authorize_admin_read(event: dict[str, Any]) -> CallerPolicy:
    policy = resolve_caller_policy(event)
    if "admin" not in policy.read_classes:
        raise ApiAuthorizationError("caller is not authorized for admin reference reads")
    return policy


def authorize_artifact_read(policy: CallerPolicy, *, artifact_class: str, binary_plan: bool = False, run_action: str | None = None) -> None:
    if run_action is not None and run_action.casefold() not in policy.actions:
        raise ApiAuthorizationError("caller is not authorized for this action")
    if binary_plan:
        if not policy.binary_plan:
            raise ApiAuthorizationError("caller is not authorized for binary plan download")
        return
    if artifact_class not in policy.artifact_classes:
        raise ApiAuthorizationError("caller is not authorized for this artifact class")
