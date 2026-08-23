# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared constants, table helpers, and exception types for the run registry.

Sibling modules in this package (``runs``, ``folders``, ``queries``) import
this module directly (``from . import _shared``) rather than importing the
package facade, to avoid an import cycle. ``dynamo_client`` and
``transact_write_items`` are re-exported here (rather than imported straight
from their defining modules by each sibling) so tests have a single
patchable location — ``run_registry._shared.dynamo_client`` /
``run_registry._shared.transact_write_items`` — that every caller observes.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.platform.aws.dynamo_codec import normalize_dynamo_item, normalize_dynamo_value
from src.platform.aws.dynamo_resource import dynamo_client, dynamo_table
from src.platform.aws.dynamo_transactions import transact_write_items

from .keys import DEFAULT_RUN_HISTORY_RETENTION_DAYS

_MAX_OUTCOME_FIELD_BYTES = 4096
_MAX_REPO_FILTER_BYTES = 255
_MAX_GATE_OBSERVATIONS = 50
# Each trigger partition gets a fixed per-request scan budget: at most eight
# DynamoDB pages and 500 evaluated rows, regardless of filter selectivity.
_MAX_RUN_LIST_EVALUATED_PAGES = 8
_MAX_RUN_LIST_EVALUATED_ITEMS = 500

_TERMINAL_RANK = 2
_RUN_CURSOR = re.compile(r"^\d{20}#[A-Za-z0-9._=-]{1,128}$")
_GATE_CURSOR = re.compile(r"^[0-9a-f]{64}$")
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class RunRegistryError(RuntimeError):
    """Raised when registry operations fail unexpectedly."""


class IdempotencyConflictError(RunRegistryError):
    """Raised when an idempotency key is reused with a different request fingerprint."""


class RunRegistryQueryError(ValueError):
    """Raised when a run-list filter or cursor is invalid."""


def _table():
    name = os.environ.get("RUN_REGISTRY_TABLE_NAME")
    if not name:
        raise RunRegistryError("RUN_REGISTRY_TABLE_NAME is not configured")
    return dynamo_table(name)


def _retention_seconds() -> int:
    raw = os.environ.get("RUN_HISTORY_RETENTION_DAYS")
    if raw is None:
        days = DEFAULT_RUN_HISTORY_RETENTION_DAYS
    else:
        try:
            days = int(raw)
        except ValueError as error:
            raise ValueError(
                f"RUN_HISTORY_RETENTION_DAYS must be an integer, got {raw!r}"
            ) from error
    return max(1, days) * 86400


def expire_ttl(now: int | None = None) -> int:
    base = now if now is not None else int(time.time())
    return base + _retention_seconds()


def is_expired(item: dict[str, Any], now: int | None = None) -> bool:
    ttl = normalize_dynamo_value(item.get("expire_ttl"))
    if not isinstance(ttl, int):
        return True
    current = now if now is not None else int(time.time())
    return ttl <= current


def _normalize(item: dict[str, Any] | None) -> dict[str, Any] | None:
    return normalize_dynamo_item(item)


def _bound_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    bounded = redact_and_bound_terminal_evidence(str(value))
    if not isinstance(bounded, str):
        raise TypeError(f"registry {label} must be a string")
    return bounded


def _bound_outcome(outcome: dict[str, Any] | None) -> dict[str, Any] | None:
    if not outcome:
        return None
    bounded: dict[str, Any] = {}
    if "succeeded" in outcome:
        bounded["succeeded"] = bool(outcome["succeeded"])
    if "credential_expired" in outcome:
        bounded["credential_expired"] = bool(outcome["credential_expired"])
    for key in ("error", "reply"):
        if key in outcome:
            text = _bound_text(outcome[key], label=key)
            if text is not None:
                bounded[key] = text
    return bounded or None


__all__ = [
    "DEFAULT_RUN_HISTORY_RETENTION_DAYS",
    "IdempotencyConflictError",
    "RunRegistryError",
    "RunRegistryQueryError",
    "dynamo_client",
    "dynamo_table",
    "expire_ttl",
    "is_expired",
    "transact_write_items",
]
