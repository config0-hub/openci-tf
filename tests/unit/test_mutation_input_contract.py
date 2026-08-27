# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation outer input carries every optional key the renderer Parameters read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.models import RepoSettings
from src.services.orchestration.start_run import (
    MUTATION_OPTIONAL_INPUT_KEYS,
    build_step_function_input,
)
from src.services.render import handler as render_handler
from src.services.webhook.run_request import github_run_request
from tests.helpers.asl_reachability import (
    render_mutation_outer_definition,
    render_read_outer_definition,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MUTATION_TF = _REPO_ROOT / "infra/deploy/modules/openci_tf/step_function_mutation_outer.tf"
_READ_TF = _REPO_ROOT / "infra/deploy/modules/openci_tf/step_function.tf"

SETTINGS = RepoSettings(
    trigger_id="trigger",
    repo_name="org/repo",
    git_url="https://github.com/org/repo.git",
    secret="secret",
    ssm_openci_tf_github_token="/openci-tf/clone-token/test",
)


def _request(action: str, *, confirm: bool):
    return github_run_request(
        {"trigger_id": "trigger", "commit_hash": "a" * 40, "pr_number": 1, "comment_id": 5},
        action=action,
        folders=[] if confirm else ["infra/a"],
        all_flag=False,
        affected_flag=False,
        delivery_id="641353f2-760d-4f70-82e9-e85611860c00",
        confirm_token="deadbeef" if confirm else None,
        intent_create=not confirm,
        intent_confirm=confirm,
    )


@pytest.mark.parametrize("action", ["apply", "destroy"])
def test_mutation_start_input_initializes_optional_renderer_keys(action):
    payload = build_step_function_input(_request(action, confirm=True), SETTINGS, "run-1")
    for key in MUTATION_OPTIONAL_INPUT_KEYS:
        assert key in payload
        assert payload[key] is None
    assert payload["confirm_token"] == "deadbeef"
    assert json.loads(json.dumps(payload))["requested_comment_id"] is None


def test_read_lane_start_input_does_not_carry_mutation_keys():
    payload = build_step_function_input(_request("apply", confirm=False), SETTINGS, "run-1")
    assert not any(key in payload for key in MUTATION_OPTIONAL_INPUT_KEYS)


def _state_machine_resource_block(source: str, resource: str) -> str:
    marker = f'resource "aws_sfn_state_machine" "{resource}"'
    return source.split(marker, 1)[1].split('\nresource "', 1)[0]


def _logging_configuration_block(source: str, resource: str) -> str:
    resource_block = _state_machine_resource_block(source, resource)
    return resource_block.split("logging_configuration {", 1)[1].split("\n  }", 1)[0]


def test_mutation_state_machine_logging_omits_execution_data_but_read_lane_keeps_it():
    mutation_source = _MUTATION_TF.read_text(encoding="utf-8")
    for resource in ("openci_tf_apply", "openci_tf_destroy"):
        block = _logging_configuration_block(mutation_source, resource)
        assert 'level                  = "ERROR"' in block
        assert "include_execution_data = false" in block
    read_block = _logging_configuration_block(
        _READ_TF.read_text(encoding="utf-8"), "openci_tf"
    )
    assert "include_execution_data = true" in read_block


def _states_referencing_keys(definition: dict) -> set[str]:
    paths = {f"$.{key}" for key in MUTATION_OPTIONAL_INPUT_KEYS}
    found: set[str] = set()
    for name, state in definition["States"].items():
        parameters = state.get("Parameters") or {}
        if any(isinstance(value, str) and value in paths for value in parameters.values()):
            found.add(name)
    return found


def _resolve_top_level_parameters(parameters: dict, state: dict) -> dict:
    resolved: dict = {}
    for key, value in parameters.items():
        if not key.endswith(".$"):
            resolved[key] = value
            continue
        output_key = key.removesuffix(".$")
        if value == "$$.Execution.Id":
            resolved[output_key] = "execution-arn"
            continue
        if not isinstance(value, str) or not value.startswith("$."):
            raise ValueError(f"unsupported test JSONPath {value!r}")
        state_key = value[2:]
        if state_key not in state:
            raise KeyError(state_key)
        resolved[output_key] = state[state_key]
    return resolved


@pytest.mark.parametrize("resource", ["openci_tf_apply", "openci_tf_destroy"])
def test_every_mutation_parameters_block_reading_optional_keys_is_fed_by_start_input(resource):
    definition = render_mutation_outer_definition(resource)
    referencing = _states_referencing_keys(definition)
    assert referencing == {
        "RenderPR",
        "RenderPlaceholder",
        "RenderPipelineFailure",
        "RenderPRFailureComment",
    }
    # Early failure routes reach RenderPipelineFailure before confirm_handler
    # has run, so the keys exist only because start_run initializes them.
    for early in ("FailParseCommand", "FailRouteAction"):
        assert definition["States"][early]["Next"] == "RenderPipelineFailure"
    for name in referencing:
        parameters = definition["States"][name]["Parameters"]
        for key in MUTATION_OPTIONAL_INPUT_KEYS:
            assert parameters[f"{key}.$"] == f"$.{key}"
    assert definition["States"]["RenderPipelineFailure"]["Parameters"]["confirm_token.$"] == "$.confirm_token"


@pytest.mark.parametrize("resource", ["openci_tf_apply", "openci_tf_destroy"])
def test_render_pr_failure_posts_failure_comment_before_finalizing(resource):
    definition = render_mutation_outer_definition(resource)
    render_pr = definition["States"]["RenderPR"]
    assert render_pr["Catch"] == [
        {
            "ErrorEquals": ["States.ALL"],
            "ResultPath": "$.render_error",
            "Next": "FailRenderPR",
        }
    ]
    assert definition["States"]["FailRenderPR"]["Next"] == "RenderPRFailureComment"
    render_failure = definition["States"]["RenderPRFailureComment"]
    assert render_failure["Parameters"]["pipeline_failure.$"] == "$.pipeline_failure"
    assert render_failure["Parameters"]["confirm_token.$"] == "$.confirm_token"
    assert render_failure["Next"] == "FinalizeAfterRenderFailure"
    assert render_failure["Catch"][0]["Next"] == "FinalizeAfterRenderFailure"


@pytest.mark.parametrize("resource", ["openci_tf_apply", "openci_tf_destroy"])
def test_render_pr_failure_parameters_resolve_after_real_config_error_normalization(resource):
    definition = render_mutation_outer_definition(resource)
    confirmed_state = {
        "webhook_info": {"repo_name": "org/repo", "pr_number": 1},
        "settings": {"ssm_openci_tf_github_token": "/openci-tf/clone-token/test"},
        "run_id": "run-1",
        "notification_target": {"type": "github_pr", "pr_number": 1},
        "action": "apply" if resource == "openci_tf_apply" else "destroy",
        "confirm_token": None,
        "consumed_confirm_token": "deadbeef",
        "requested_comment_id": 10,
        "requested_comment_body": "tf apply infra/a",
        "intent_comment_id": 11,
    }
    normalized = render_handler.handler(
        {"normalize_config_error": True, "state": confirmed_state}, object()
    )
    fallback_state = {
        **normalized,
        "pipeline_failure": {"failed_step": "RenderPR"},
    }

    resolved = _resolve_top_level_parameters(
        definition["States"]["RenderPRFailureComment"]["Parameters"],
        fallback_state,
    )

    assert resolved["confirm_token"] is None
    assert resolved["consumed_confirm_token"] == "deadbeef"
    assert resolved["pipeline_failure"] == {"failed_step": "RenderPR"}


def test_read_outer_parameters_do_not_reference_mutation_keys():
    assert _states_referencing_keys(render_read_outer_definition()) == set()


def test_mutation_tf_source_references_only_the_initialized_keys():
    source = _MUTATION_TF.read_text(encoding="utf-8")
    for key in MUTATION_OPTIONAL_INPUT_KEYS:
        assert f'"{key}.$"' in source
