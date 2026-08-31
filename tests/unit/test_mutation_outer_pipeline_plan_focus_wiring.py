# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation outer ItemSelector → inner CollectMutation pipeline_plan_focus wiring."""

from __future__ import annotations

import pytest

from src.domain.engine.inner_run_folder_state import collect_task_parameters
from src.domain.engine.manifest import _artifact_names_for_action
from src.domain.engine.outer_map_state import compact_map_item, merge_map_item
from src.services.run_folder import persist_retry_attempt
from tests.helpers.asl_reachability import render_mutation_outer_definition
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition

_UPSTREAMS = {
    "tofu": "https://downloads.example/tofu",
    "tfsec": "https://downloads.example/tfsec",
    "infracost": "https://downloads.example/infracost",
}


def _binding() -> list[object]:
    return [
        "openci-tf-executor-readonly",
        None,
        "openci-tf-0123456789abcdef",
        3600,
    ]


def _folder_pin() -> dict:
    return {
        "source_run_id": "1788127349213.7e34ddd6",
        "plan_sha256": "a" * 64,
        "plan_artifact_name": "plan.tfplan",
        "account_id": "123456789012",
        "account_binding": _binding(),
        "tf_runtime": "tofu:1.10.6",
    }


def _compact_mutation_item(
    folder: str,
    *,
    action: str = "apply",
    run_id: str = "r" * 32,
    step_index: int = 0,
) -> dict:
    return compact_map_item(
        {
            "run_id": run_id,
            "folder": folder,
            "account_id": "123456789012",
            "account_binding": _binding(),
            "action": action,
            "attempt": 0,
            "budget": 3600,
            "deadline_at": "2099-01-01T00:00:00Z",
            "folder_config": {"account_alias": "target"},
            "upstream_urls": _UPSTREAMS,
            "execution_id": f"{run_id}.{folder}.0",
            "repo_name": "org/repo",
            "git_url": "https://github.com/org/repo.git",
            "commit_hash": "a" * 40,
            "ssm_openci_tf_github_token": "/openci-tf/github/token",
            "ssm_infracost_api_key": "/openci-tf/infracost/key",
            "step_index": step_index,
            "pipeline_plan_focus": False,
            "folder_pin": _folder_pin(),
            "source_plan_run_id": "1788127349213.7e34ddd6",
            "grace_seconds": 0,
            "command_context": {"pipeline": "mutation-e625cc1", "pipeline_step": 1},
        }
    )


def _map_shared() -> dict:
    return {
        "upstream_urls": _UPSTREAMS,
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
    }


def _resolve_item_selector(item_selector: dict, *, state: dict, map_item: dict) -> dict:
    resolved: dict = {}
    for key, value in item_selector.items():
        if not key.endswith(".$"):
            resolved[key] = value
            continue
        output_key = key.removesuffix(".$")
        if value == "$.step_index":
            if "step_index" not in state:
                raise KeyError("step_index")
            resolved[output_key] = state["step_index"]
            continue
        if value.startswith("$$.Map.Item.Value."):
            item_key = value.split("$$.Map.Item.Value.", 1)[1]
            if item_key not in map_item:
                raise KeyError(item_key)
            resolved[output_key] = map_item[item_key]
            continue
        if value.startswith("$.map_shared."):
            shared_key = value.split("$.map_shared.", 1)[1]
            map_shared = state.get("map_shared")
            if not isinstance(map_shared, dict) or shared_key not in map_shared:
                raise KeyError(shared_key)
            resolved[output_key] = map_shared[shared_key]
            continue
        raise ValueError(f"unsupported test JSONPath {value!r}")
    return resolved


def _resolve_collect_parameters(parameters: dict, state: dict) -> dict:
    resolved: dict = {}
    for key, value in parameters.items():
        if not key.endswith(".$"):
            resolved[key] = value
            continue
        output_key = key.removesuffix(".$")
        if value.startswith("$.probe."):
            probe_key = value.split("$.probe.", 1)[1]
            if probe_key not in state["probe"]:
                raise KeyError(probe_key)
            resolved[output_key] = state["probe"][probe_key]
            continue
        if value.startswith("$."):
            state_key = value[2:]
            if state_key not in state:
                raise KeyError(state_key)
            resolved[output_key] = state[state_key]
            continue
        raise ValueError(f"unsupported test JSONPath {value!r}")
    return resolved


def _probe_result() -> dict:
    return {
        "exec_id": "1788127106246.faf33c46",
        "attempt": 0,
        "succeeded": True,
        "credential_expired": False,
        "steps": [],
        "error": None,
        "pointers": {"done": "s3://done/1788127106246.faf33c46/done"},
        "submitted_at": 1_700_000_000.0,
    }


def _assert_mutation_collect_resolves(
    *,
    lane: str,
    outer_resource: str,
    action: str,
    map_items: list[dict],
    step_index: int = 0,
) -> None:
    outer = render_mutation_outer_definition(outer_resource)
    inner = load_rendered_run_folder_definition(lane)
    collect_parameters = inner["States"]["CollectMutation"]["Parameters"]
    state = {
        "map_shared": _map_shared(),
        "map_items": map_items,
        "step_index": step_index,
    }
    item_selector = outer["States"]["RunFoldersSequential"]["ItemSelector"]
    for map_item in map_items:
        inner_input = _resolve_item_selector(
            item_selector, state=state, map_item=map_item
        )
        assert inner_input["action"] == action
        assert inner_input["pipeline_plan_focus"] is False
        inner_state = {**inner_input, "probe": _probe_result()}
        resolved_collect = _resolve_collect_parameters(collect_parameters, inner_state)
        assert resolved_collect["pipeline_plan_focus"] is False
        mirrored = collect_task_parameters(inner_state, mutation=True)
        assert mirrored["pipeline_plan_focus"] is False
        assert mirrored["source_plan_run_id"] == inner_input["source_plan_run_id"]


@pytest.mark.parametrize(
    ("lane", "outer_resource", "action"),
    [
        ("apply", "openci_tf_apply", "apply"),
        ("destroy", "openci_tf_destroy", "destroy"),
    ],
)
def test_mutation_outer_item_selector_threads_pipeline_plan_focus_false(
    lane: str, outer_resource: str, action: str
):
    block = (
        __import__("pathlib").Path("infra/deploy/modules/openci_tf/step_function_mutation_outer.tf")
        .read_text(encoding="utf-8")
        .split("RunFoldersSequential = {", 1)[1]
        .split("\n      }", 1)[0]
    )
    assert '"pipeline_plan_focus.$"' in block
    assert '"$$.Map.Item.Value.pipeline_plan_focus"' in block
    map_item = _compact_mutation_item("infra/a", action=action)
    _assert_mutation_collect_resolves(
        lane=lane,
        outer_resource=outer_resource,
        action=action,
        map_items=[map_item],
    )


def test_pipeline_mutation_confirm_items_resolve_collect_with_focus_false():
    map_items = [
        _compact_mutation_item("infra/vpc", action="apply", step_index=0),
        _compact_mutation_item("infra/rds", action="apply", step_index=1),
    ]
    _assert_mutation_collect_resolves(
        lane="apply",
        outer_resource="openci_tf_apply",
        action="apply",
        map_items=map_items,
        step_index=1,
    )


def test_mutation_credential_retry_preserves_pipeline_plan_focus_false():
    base = {
        **_map_shared(),
        **merge_map_item(_map_shared(), _compact_mutation_item("infra/a")),
        "result": {"attempt": 0, "exec_id": "run.infra.0", "submitted_at": 1.0},
        "attempt": 0,
    }
    for event in (
        {"event": {**base, "probe": {"attempt": 0, "exec_id": "run.infra.0", "submitted_at": 1.0}}},
        {"event": {key: value for key, value in base.items() if key != "result"}, "execution_started_at": "2026-01-01T00:00:00Z"},
    ):
        retried = persist_retry_attempt.CredentialRetry.from_event(event).resubmit_state()
        assert retried["pipeline_plan_focus"] is False
        inner = {**retried, "probe": _probe_result()}
        params = collect_task_parameters(inner, mutation=True)
        assert params["pipeline_plan_focus"] is False


@pytest.mark.parametrize("action", ["apply", "destroy"])
def test_mutation_manifest_requires_mutation_artifacts_not_tfsec(action: str):
    names = _artifact_names_for_action(action, pipeline_plan_focus=False)
    assert "tfsec.json" not in names
    assert "infracost.json" not in names
    if action == "apply":
        assert "apply.out" in names
        assert "plan-show.out" in names
    else:
        assert "destroy.out" in names
        assert "plan-show.out" in names


def test_read_focus_true_false_unchanged_by_mutation_fix():
    focused = _artifact_names_for_action("plan", pipeline_plan_focus=True)
    regular = _artifact_names_for_action("plan", pipeline_plan_focus=False)
    assert "tfsec.json" not in focused
    assert "tfsec.json" in regular
    assert merge_map_item(_map_shared(), {"run_id": "r", "folder": "f", "account_id": "1", "action": "plan", "attempt": 0, "budget": 1, "deadline_at": "t", "b": _binding(), "c": {}, "e": "e", "pipeline_plan_focus": True})["pipeline_plan_focus"] is True
    assert merge_map_item(_map_shared(), {"run_id": "r", "folder": "f", "account_id": "1", "action": "plan", "attempt": 0, "budget": 1, "deadline_at": "t", "b": _binding(), "c": {}, "e": "e"})["pipeline_plan_focus"] is False


def test_live_regression_missing_pipeline_plan_focus_on_inner_input_fails_collect():
    """Reproduce e625cc1 defect: CollectMutation JSONPath without producer field."""
    inner = load_rendered_run_folder_definition("apply")
    collect_parameters = inner["States"]["CollectMutation"]["Parameters"]
    inner_state = {
        "action": "apply",
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "account_id": "123456789012",
        "folder": "infra/a",
        "run_id": "r" * 32,
        "deadline_at": "2099-01-01T00:00:00Z",
        "source_plan_run_id": "1788127349213.7e34ddd6",
        "step_index": 0,
        "probe": _probe_result(),
    }
    with pytest.raises(KeyError, match="pipeline_plan_focus"):
        _resolve_collect_parameters(collect_parameters, inner_state)
