# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Serialized inner run-folder Step Functions state budgets and ASL helpers."""
from __future__ import annotations

import json
from typing import Any

from src.core.errors import ConfigResolutionError
from src.domain.engine.artifact_limits import (
    MAX_INNER_STATE_BYTES,
    MAX_POLL_DONE_RESULT_BYTES,
    STEP_FUNCTIONS_STATE_LIMIT,
)

_REQUIRED_MAP_ITEM_FIELDS = frozenset(
    {
        "run_id",
        "folder",
        "account_id",
        "account_binding",
        "action",
        "attempt",
        "budget",
        "deadline_at",
        "folder_config",
        "upstream_urls",
        "execution_id",
        "repo_name",
        "git_url",
        "commit_hash",
        "ssm_openci_tf_github_token",
        "ssm_infracost_api_key",
    }
)


def serialized_state_bytes(state: object) -> int:
    """Return compact JSON byte length as Step Functions measures task I/O."""
    return len(json.dumps(state, separators=(",", ":")).encode())


def apply_result_path(state: dict[str, Any], result_path: str, value: object) -> dict[str, Any]:
    """Apply the two rendered task result paths in the explicit probe flow."""
    if result_path == "$.result":
        return {**state, "result": value}
    if result_path == "$.probe":
        return {**state, "probe": value}
    raise ValueError(f"unsupported result path: {result_path}")


def post_poll_done_state(state: dict[str, Any], probe_result: dict[str, Any]) -> dict[str, Any]:
    """Return the inner state after ProbeDone ``ResultPath = $.probe``."""
    return apply_result_path(state, "$.probe", probe_result)


def max_accepted_poll_result_bytes(map_item: dict[str, Any]) -> int:
    """Remaining single-probe payload budget for a validated map item."""
    base = serialized_state_bytes(map_item)
    remaining = MAX_INNER_STATE_BYTES - base
    return min(MAX_POLL_DONE_RESULT_BYTES, max(0, remaining))


def assert_post_poll_state_within_budget(
    map_item: dict[str, Any],
    poll_result: dict[str, Any],
    *,
    limit: int = MAX_INNER_STATE_BYTES,
) -> dict[str, Any]:
    """Prove a ProbeDone replacement stays below the configured inner-state budget."""
    post = post_poll_done_state(map_item, poll_result)
    size = serialized_state_bytes(post)
    if size > limit:
        raise ValueError(
            f"post-ResultPath inner state exceeds budget: {size} bytes > {limit} bytes"
        )
    return post


def validate_inner_map_item(item: dict[str, Any]) -> None:
    """Reject map items that cannot survive a probe result without a state-size failure."""
    missing = sorted(_REQUIRED_MAP_ITEM_FIELDS - item.keys())
    if missing:
        raise ConfigResolutionError(f"inner map item missing fields: {', '.join(missing)}")
    size = serialized_state_bytes(item)
    if size > MAX_INNER_STATE_BYTES - MAX_POLL_DONE_RESULT_BYTES:
        raise ConfigResolutionError(
            f"folder configuration exceeds inner Step Functions state budget ({size} bytes)"
        )
    if max_accepted_poll_result_bytes(item) < 512:
        raise ConfigResolutionError(
            "folder configuration leaves insufficient Step Functions state headroom"
        )


def inner_state_budget_summary() -> dict[str, int]:
    """Expose the configured inner-state seam for tests and audits."""
    return {
        "step_functions_limit": STEP_FUNCTIONS_STATE_LIMIT,
        "max_inner_state_bytes": MAX_INNER_STATE_BYTES,
        "max_poll_done_result_bytes": MAX_POLL_DONE_RESULT_BYTES,
        "headroom_bytes": STEP_FUNCTIONS_STATE_LIMIT - MAX_INNER_STATE_BYTES,
    }
