"""Deterministic, time-sortable outer orchestration run identifiers."""

from __future__ import annotations

import hashlib
import re
import time

from src.domain.engine.invocation_id import assert_execution_id_bounds

_LEGACY_UUID = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_EPOCH_HASH = re.compile(r"^(\d{1,20})\.([0-9a-f]{8})$", re.IGNORECASE)
_MANAGED_ACTIONS = frozenset(
    {"plan", "drift", "report", "plan_destroy", "apply", "destroy"}
)


def _action_hash_token(repo_name: str, action: str) -> str:
    if not repo_name or not action:
        raise ValueError("repo_name and action are required")
    if action not in _MANAGED_ACTIONS:
        raise ValueError(f"unsupported outer execution action: {action}")
    material = f"{repo_name}.{action}"
    return hashlib.sha256(material.encode()).hexdigest()[:8]


def compose_outer_run_id(
    repo_name: str, action: str, *, epoch_ms: int | None = None
) -> str:
    """Mint a new outer run id before Step Functions start."""
    when = epoch_ms if epoch_ms is not None else int(time.time() * 1000)
    if when < 1:
        raise ValueError("epoch_ms must be positive")
    run_id = f"{when}.{_action_hash_token(repo_name, action)}"
    assert_execution_id_bounds(run_id)
    return run_id


def is_legacy_uuid_run_id(run_id: str) -> bool:
    return bool(_LEGACY_UUID.fullmatch(str(run_id or "").strip()))


def parse_outer_run_epoch(run_id: str) -> int | None:
    """Return epoch milliseconds from a deterministic outer id, else None."""
    match = _EPOCH_HASH.fullmatch(str(run_id or "").strip())
    if match is None:
        return None
    return int(match.group(1))


def validate_outer_run_id(run_id: str) -> str:
    text = str(run_id or "").strip()
    if not text:
        raise ValueError("run_id is required")
    if is_legacy_uuid_run_id(text):
        assert_execution_id_bounds(text)
        return text.lower()
    if _EPOCH_HASH.fullmatch(text):
        assert_execution_id_bounds(text)
        return text
    raise ValueError("run_id must be a legacy UUID or epoch-hash outer id")
