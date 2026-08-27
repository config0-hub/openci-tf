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
from src.services.webhook.run_request import github_run_request
from tests.helpers.asl_reachability import (
    render_mutation_outer_definition,
    render_read_outer_definition,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MUTATION_TF = _REPO_ROOT / "infra/deploy/modules/openci_tf/step_function_mutation_outer.tf"

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


def _states_referencing_keys(definition: dict) -> set[str]:
    paths = {f"$.{key}" for key in MUTATION_OPTIONAL_INPUT_KEYS}
    found: set[str] = set()
    for name, state in definition["States"].items():
        parameters = state.get("Parameters") or {}
        if any(isinstance(value, str) and value in paths for value in parameters.values()):
            found.add(name)
    return found


@pytest.mark.parametrize("resource", ["openci_tf_apply", "openci_tf_destroy"])
def test_every_mutation_parameters_block_reading_optional_keys_is_fed_by_start_input(resource):
    definition = render_mutation_outer_definition(resource)
    referencing = _states_referencing_keys(definition)
    assert referencing == {"RenderPR", "RenderPlaceholder", "RenderPipelineFailure"}
    # Early failure routes reach RenderPipelineFailure before confirm_handler
    # has run, so the keys exist only because start_run initializes them.
    for early in ("FailParseCommand", "FailRouteAction"):
        assert definition["States"][early]["Next"] == "RenderPipelineFailure"
    for name in referencing:
        parameters = definition["States"][name]["Parameters"]
        for key in MUTATION_OPTIONAL_INPUT_KEYS:
            assert parameters[f"{key}.$"] == f"$.{key}"
    assert definition["States"]["RenderPipelineFailure"]["Parameters"]["confirm_token.$"] == "$.confirm_token"


def test_read_outer_parameters_do_not_reference_mutation_keys():
    assert _states_referencing_keys(render_read_outer_definition()) == set()


def test_mutation_tf_source_references_only_the_initialized_keys():
    source = _MUTATION_TF.read_text(encoding="utf-8")
    for key in MUTATION_OPTIONAL_INPUT_KEYS:
        assert f'"{key}.$"' in source
