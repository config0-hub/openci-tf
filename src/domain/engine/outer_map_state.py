# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Outer ValidateAndResolve Step Functions state budgeting and compaction."""
from __future__ import annotations

import json
from typing import Any

from src.core.errors import ConfigResolutionError
from src.domain.engine.artifact_limits import (
    MAX_OUTER_CHILD_ERROR_CHARS,
    MAX_OUTER_FOLDER_CONFIG_SERIALIZED_BYTES,
    MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES,
    MAX_OUTER_POST_MAP_STATE_BYTES,
    MAX_OUTER_VALIDATE_BYTES,
    STEP_FUNCTIONS_STATE_LIMIT,
)
from src.domain.config.folder_config import expand_folder_config_from_outer_state
from src.domain.engine.artifact_paths import build_folder_artifact_keys, manifest_key
from src.domain.engine.deployment_buckets import maximum_foundation_bucket_names
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.inner_state import serialized_state_bytes
from src.domain.engine.plan_artifacts import expected_plan_artifact_uris
from src.domain.engine.result import ExecutionResult
from src.domain.engine.summary import (
    bounded_summary,
    build_outer_map_outcome,
    validate_outer_map_outcome,
)

SHARED_MAP_FIELDS = frozenset(
    {
        "upstream_urls",
        "repo_name",
        "git_url",
        "commit_hash",
        "ssm_openci_tf_github_token",
        "ssm_infracost_api_key",
    }
)

COMPACT_MAP_ITEM_FIELDS = frozenset(
    {
        "run_id",
        "folder",
        "account_id",
        "action",
        "attempt",
        "budget",
        "deadline_at",
    }
)

OPTIONAL_COMPACT_MAP_ITEM_FIELDS = frozenset(
    {
        "folder_pin",
        "source_plan_run_id",
        "grace_seconds",
        "step_index",
        "command_context",
    }
)

BOUNDED_TASK_CATCH_ERROR = "nested execution failed"
BOUNDED_MAP_CATCH_ERROR = "map execution failed"
BOUNDED_RENDER_CATCH_ERROR = "render failed"


def validate_folder_config_outer_size(folder_config: object) -> None:
    """Reject folder configs that cannot fit fifty maximum items in outer state."""
    if not isinstance(folder_config, dict):
        raise ConfigResolutionError("folder_config must be an object")
    size = len(json.dumps(folder_config, separators=(",", ":")).encode())
    if size > MAX_OUTER_FOLDER_CONFIG_SERIALIZED_BYTES:
        raise ConfigResolutionError(
            f"folder configuration exceeds outer aggregate budget ({size} bytes)"
        )


def merge_map_item(map_shared: dict[str, Any], compact_item: dict[str, Any]) -> dict[str, Any]:
    """Rehydrate the inner run-folder input from shared and compact map state."""
    merged = {field: map_shared[field] for field in SHARED_MAP_FIELDS} | {
        field: compact_item[field] for field in COMPACT_MAP_ITEM_FIELDS
    }
    merged["account_binding"] = compact_item["b"]
    merged["execution_id"] = compact_item["e"]
    merged["folder_config"] = expand_folder_config_from_outer_state(compact_item["c"])
    for field in OPTIONAL_COMPACT_MAP_ITEM_FIELDS:
        if field in compact_item:
            merged[field] = compact_item[field]
    raw_step_index = merged.get("step_index")
    if not isinstance(raw_step_index, int) or isinstance(raw_step_index, bool):
        merged["step_index"] = 0
    return merged


def compact_map_item(full_item: dict[str, Any]) -> dict[str, Any]:
    """Strip repo-wide fields duplicated across every Map item."""
    compact = {field: full_item[field] for field in COMPACT_MAP_ITEM_FIELDS}
    compact["b"] = full_item["account_binding"]
    compact["c"] = full_item["folder_config"]
    compact["e"] = full_item["execution_id"]
    for field in OPTIONAL_COMPACT_MAP_ITEM_FIELDS:
        if field in full_item:
            compact[field] = full_item[field]
    return compact


def _shared_context(full_items: list[dict[str, Any]]) -> dict[str, Any]:
    if not full_items:
        raise ConfigResolutionError("map items required for shared context")
    shared = {field: full_items[0][field] for field in SHARED_MAP_FIELDS}
    for item in full_items[1:]:
        for field in SHARED_MAP_FIELDS:
            if item[field] != shared[field]:
                raise ConfigResolutionError(f"inconsistent shared map field {field}")
    return shared


def assert_outer_state_within_budget(state: dict[str, Any], *, stage: str) -> None:
    """Reject outer payloads that would overflow Step Functions at ``stage``."""
    limit = MAX_OUTER_VALIDATE_BYTES if stage == "validate-and-resolve" else MAX_OUTER_POST_MAP_STATE_BYTES
    size = serialized_state_bytes(state)
    if size > limit:
        raise ConfigResolutionError(
            f"outer state exceeds Step Functions budget at {stage} ({size} bytes)"
        )


def apply_placeholder_transition(state: dict[str, Any], placeholder_result: object) -> dict[str, Any]:
    """RenderPlaceholder uses ``ResultPath = null``; acknowledge without duplicating state."""
    if placeholder_result not in (None, {}):
        rendered = placeholder_result.get("placeholder_rendered") if isinstance(placeholder_result, dict) else None
        if rendered is not True:
            raise ConfigResolutionError("placeholder transition must not duplicate outer state")
    return state


def apply_map_outcomes_transition(state: dict[str, Any], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the outer Map ``ResultPath = $.outcomes`` replacement."""
    for outcome in outcomes:
        validate_outer_map_outcome(outcome)
    aggregate = serialized_state_bytes(outcomes)
    if aggregate > MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES:
        raise ConfigResolutionError(
            f"outer map outcomes exceed aggregate budget ({aggregate} bytes)"
        )
    post = {**state, "outcomes": outcomes}
    assert_outer_state_within_budget(post, stage="post-map")
    return post


def apply_render_pr_catch_transition(state: dict[str, Any]) -> dict[str, Any]:
    """RenderPR catch uses ``ResultPath = null``; the post-map state is unchanged."""
    assert_outer_state_within_budget(state, stage="render-pr-catch")
    return state


def apply_finalize_run_input_transition(state: dict[str, Any]) -> dict[str, Any]:
    """FinalizeRun receives the bounded post-map or post-catch state as task input."""
    assert_outer_state_within_budget(state, stage="finalize-run-input")
    return state


def apply_finalize_run_result_transition(state: dict[str, Any], finalizer_result: object) -> dict[str, Any]:
    """FinalizeRun uses ``ResultPath = null`` and returns only a tiny acknowledgement."""
    if not isinstance(finalizer_result, dict) or finalizer_result != {"finalized": True}:
        raise ConfigResolutionError("finalizer transition must not duplicate outer state")
    assert_outer_state_within_budget(state, stage="finalize-run-result")
    return state


def render_pr_input_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded RenderPR task input derived from outer state."""
    return {
        "webhook_info": state["webhook_info"],
        "settings": state["settings"],
        "action": state["action"],
        "run_id": state["run_id"],
        "notification_target": state.get("notification_target"),
        "outcomes": state.get("outcomes", []),
        "skipped": state.get("skipped", []),
        "execution_arn": state.get("execution_arn"),
    }


def _production_buckets() -> tuple[str, str]:
    buckets = maximum_foundation_bucket_names()
    return buckets["tmp"], buckets["done"]


def _maximum_child_success_output(item: dict[str, Any], *, tmp_bucket: str, done_bucket: str) -> dict[str, Any]:
    folder = str(item["folder"])
    exec_id = str(item.get("execution_id") or compose_execution_id(str(item["run_id"]), folder, int(item.get("attempt") or 0)))
    repo_name = str(item.get("repo_name") or "")
    run_id = str(item["run_id"])
    attempt = int(item.get("attempt") or 0)
    folder_keys = build_folder_artifact_keys(repo_name=repo_name, run_id=run_id, folder_path=folder)
    plan_metadata = expected_plan_artifact_uris(
        bucket=tmp_bucket,
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
    ).metadata
    pointers = {
        "artifacts_prefix": f"s3://{tmp_bucket}/{folder_keys.prefix}",
        "done": f"s3://{done_bucket}/{exec_id}/done",
        "plan_metadata": plan_metadata,
    }
    summary = bounded_summary(
        ExecutionResult(exec_id, True, [], None),
        pointers,
        attempt=attempt,
    )
    summary["manifest_s3_uri"] = f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}"
    summary["manifest_sha256"] = "a" * 64
    return summary


def _maximum_child_failure_output(
    item: dict[str, Any], *, tmp_bucket: str, done_bucket: str
) -> dict[str, Any]:
    folder = str(item["folder"])
    exec_id = str(item.get("execution_id") or compose_execution_id(str(item["run_id"]), folder, int(item.get("attempt") or 0)))
    repo_name = str(item.get("repo_name") or "")
    run_id = str(item["run_id"])
    attempt = int(item.get("attempt") or 0)
    folder_keys = build_folder_artifact_keys(repo_name=repo_name, run_id=run_id, folder_path=folder)
    plan_metadata = expected_plan_artifact_uris(
        bucket=tmp_bucket,
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
    ).metadata
    pointers = {
        "artifacts_prefix": f"s3://{tmp_bucket}/{folder_keys.prefix}",
        "done": f"s3://{done_bucket}/{exec_id}/done",
        "plan_metadata": plan_metadata,
    }
    summary = bounded_summary(
        ExecutionResult(exec_id, False, [], "x" * MAX_OUTER_CHILD_ERROR_CHARS),
        pointers,
        attempt=attempt,
    )
    summary["manifest_s3_uri"] = f"s3://{tmp_bucket}/{manifest_key(repo_name, run_id, folder)}"
    summary["manifest_sha256"] = "a" * 64
    return summary


def _project_production_outcomes(
    map_items: list[dict[str, Any]],
    *,
    shape: str,
    map_shared: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    tmp_bucket, done_bucket = _production_buckets()
    outcomes: list[dict[str, Any]] = []
    for item in map_items:
        folder = str(item["folder"])
        account_id = str(item.get("account_id") or "")
        execution_id = str(
            item.get("execution_id")
            or compose_execution_id(str(item["run_id"]), folder, int(item.get("attempt") or 0))
        )
        enriched = {**item, **(map_shared or {})}
        step_index = item.get("step_index") if isinstance(item.get("step_index"), int) else None
        if shape == "success":
            output = _maximum_child_success_output(enriched, tmp_bucket=tmp_bucket, done_bucket=done_bucket)
            outcomes.append(
                build_outer_map_outcome(
                    folder=folder,
                    account_id=account_id,
                    execution_id=execution_id,
                    output=output,
                    step_index=step_index,
                )
            )
        elif shape == "failure":
            output = _maximum_child_failure_output(
                enriched, tmp_bucket=tmp_bucket, done_bucket=done_bucket
            )
            outcomes.append(
                build_outer_map_outcome(
                    folder=folder,
                    account_id=account_id,
                    execution_id=execution_id,
                    output=output,
                    step_index=step_index,
                )
            )
        elif shape == "malformed":
            outcomes.append(
                build_outer_map_outcome(
                    folder=folder,
                    account_id=account_id,
                    execution_id=execution_id,
                    status="infrastructure_error",
                    error="malformed child execution output",
                    step_index=step_index,
                )
            )
        elif shape == "task-catch":
            outcomes.append(
                build_outer_map_outcome(
                    folder=folder,
                    account_id=account_id,
                    execution_id=execution_id,
                    status="infrastructure_error",
                    error=BOUNDED_TASK_CATCH_ERROR,
                    attempt=int(item.get("attempt") or 0),
                    step_index=step_index,
                )
            )
        elif shape == "map-catch":
            outcomes.append(
                build_outer_map_outcome(
                    folder=folder,
                    account_id=account_id,
                    execution_id=execution_id,
                    status="infrastructure_error",
                    error=BOUNDED_MAP_CATCH_ERROR,
                    attempt=int(item.get("attempt") or 0),
                    step_index=step_index,
                )
            )
        else:
            raise ConfigResolutionError(f"unknown production outcome shape {shape}")
    return outcomes


def validate_outer_transition_sequence(
    *,
    validate_output: dict[str, Any],
    placeholder_result: object = None,
    outcomes: list[dict[str, Any]] | None = None,
) -> None:
    """Budget the rendered outer ASL transition sequence for accepted input."""
    assert_outer_state_within_budget(validate_output, stage="validate-and-resolve")
    post_placeholder = apply_placeholder_transition(validate_output, placeholder_result)
    assert_outer_state_within_budget(post_placeholder, stage="post-placeholder")
    if outcomes is None:
        return
    post_map = apply_map_outcomes_transition(post_placeholder, outcomes)
    render_input = render_pr_input_state(post_map)
    assert_outer_state_within_budget(render_input, stage="render-pr-input")
    post_render_catch = apply_render_pr_catch_transition(post_map)
    finalize_input = apply_finalize_run_input_transition(post_render_catch)
    apply_finalize_run_result_transition(finalize_input, {"finalized": True})


def validate_outer_resolve_result(result: dict[str, Any]) -> None:
    """Reject ValidateAndResolve payloads that would overflow Step Functions state."""
    assert_outer_state_within_budget(result, stage="validate-and-resolve")
    map_items = result.get("map_items")
    map_shared = result.get("map_shared") if isinstance(result.get("map_shared"), dict) else None
    if isinstance(map_items, list) and map_items:
        for shape in ("success", "failure", "malformed", "task-catch", "map-catch"):
            validate_outer_transition_sequence(
                validate_output=result,
                placeholder_result={"placeholder_rendered": True},
                outcomes=_project_production_outcomes(map_items, shape=shape, map_shared=map_shared),
            )


def build_compact_resolve_result(
    event: dict[str, Any],
    *,
    run_id: str,
    full_items: list[dict[str, Any]],
    skipped: list[dict[str, str]],
) -> dict[str, Any]:
    """Return the bounded outer payload with shared fields stored once."""
    if not full_items:
        result = {
            **event,
            "run_id": run_id,
            "map_items": [],
            "current_step_items": [],
            "steps": event.get("steps", []),
            "step_index": 0,
            "step_count": 0,
            "outcomes": [],
            "skipped": skipped,
        }
        validate_outer_resolve_result(result)
        return result
    map_shared = _shared_context(full_items)
    compact_items = [compact_map_item(item) for item in full_items]
    pipeline_run = isinstance(event.get("pipeline"), str)
    result = {
        **event,
        "run_id": run_id,
        "map_shared": map_shared,
        "map_items": compact_items,
        "step_index": 0,
        "step_count": 0,
        "outcomes": [],
        "skipped": skipped,
    }
    if pipeline_run:
        steps = _resolved_steps(event, compact_items)
        result["steps"] = steps
        result["step_count"] = len(steps)
        result["current_step_items"] = _items_for_step(
            compact_items, 0, steps=steps
        )
    else:
        result["steps"] = []
    validate_outer_resolve_result(result)
    return result


def _resolved_steps(event: dict[str, Any], items: list[dict[str, Any]]) -> list[list[str]]:
    steps = event.get("steps")
    if isinstance(steps, list) and steps:
        return steps
    folders = [str(item["folder"]) for item in items]
    if not folders:
        return []
    return [folders]


def _items_for_step(
    items: list[dict[str, Any]],
    step_index: int,
    *,
    steps: list[list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return compact map items for one pipeline step with the cursor step_index stamped."""
    if steps is not None and 0 <= step_index < len(steps):
        folders = frozenset(steps[step_index])
        selected = [item for item in items if item.get("folder") in folders]
    else:
        selected = [item for item in items if item.get("step_index") == step_index]
    return [{**item, "step_index": step_index} for item in selected]


def outer_state_budget_summary() -> dict[str, int]:
    """Expose configured outer-state seams for tests and audits."""
    return {
        "max_outer_validate_bytes": MAX_OUTER_VALIDATE_BYTES,
        "max_outer_post_map_state_bytes": MAX_OUTER_POST_MAP_STATE_BYTES,
        "max_outer_folder_config_bytes": MAX_OUTER_FOLDER_CONFIG_SERIALIZED_BYTES,
        "max_outer_map_aggregate_outcomes_bytes": MAX_OUTER_MAP_AGGREGATE_OUTCOMES_BYTES,
        "headroom_bytes": STEP_FUNCTIONS_STATE_LIMIT - MAX_OUTER_POST_MAP_STATE_BYTES,
    }
