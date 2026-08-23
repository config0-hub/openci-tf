# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic ASL reachability for outer Step Functions definitions."""
from __future__ import annotations

import pytest

from tests.helpers.asl_reachability import (
    dangling_transitions,
    invalid_choice_states,
    render_mutation_outer_definition,
    render_read_outer_definition,
    unreachable_states,
)


@pytest.mark.parametrize(
    "definition_loader",
    [
        render_read_outer_definition,
        lambda: render_mutation_outer_definition("openci_tf_apply"),
        lambda: render_mutation_outer_definition("openci_tf_destroy"),
    ],
    ids=["read", "apply", "destroy"],
)
def test_outer_definition_states_are_reachable(definition_loader):
    definition = definition_loader()
    unreachable = unreachable_states(definition)
    assert unreachable == [], f"unreachable states: {unreachable}"
    dangling = dangling_transitions(definition)
    assert dangling == [], f"dangling transitions: {dangling}"
    invalid_choices = invalid_choice_states(definition)
    assert invalid_choices == [], f"Choice states without Choices: {invalid_choices}"


def test_read_outer_retains_intent_create_not_confirm():
    definition = render_read_outer_definition()
    states = definition["States"]
    assert "ConfirmApplyIntent" not in states
    assert "ConfirmDestroyIntent" not in states
    assert "RouteAfterConfirm" not in states
    assert "CreateIntent" in states
    assert "CreateApplyIntent" not in states
    assert "CreateDestroyIntent" not in states
    assert "RouteAfterIntent" in states


@pytest.mark.parametrize(
    ("definition_loader", "top_level_count", "nested_count", "removed"),
    [
        (
            render_read_outer_definition,
            29,
            4,
            {
                "RenderEarlyPlaceholder",
                "RouteResolved",
                "RenderNoOp",
                "FailRenderNoOp",
                "RouteAfterFinalize",
                "ConfigResolutionFailed",
                "CreateApplyIntent",
                "CreateDestroyIntent",
                "FailCreateApplyIntent",
                "FailCreateDestroyIntent",
            },
        ),
        (
            lambda: render_mutation_outer_definition("openci_tf_apply"),
            23,
            5,
            {"RenderEarlyPlaceholder"},
        ),
        (
            lambda: render_mutation_outer_definition("openci_tf_destroy"),
            23,
            5,
            {"RenderEarlyPlaceholder"},
        ),
    ],
    ids=["read", "apply", "destroy"],
)
def test_rendered_outer_state_counts_and_removed_states(
    definition_loader, top_level_count, nested_count, removed
):
    definition = definition_loader()
    states = definition["States"]
    assert len(states) == top_level_count
    assert (
        sum(
            len(state.get("Iterator", {}).get("States", {}))
            for state in states.values()
        )
        == nested_count
    )
    assert removed.isdisjoint(states)


def test_rendered_read_iterator_delegates_all_child_envelopes_to_one_consumer():
    definition = render_read_outer_definition()
    top_level_states = definition["States"]
    assert top_level_states["NormalizeConfigError"]["Type"] == "Task"
    assert top_level_states["NormalizeConfigError"]["Parameters"] == {
        "normalize_config_error": True,
        "state.$": "$",
    }
    assert top_level_states["NextStep"]["Choices"][0]["Next"] == "RunStepFolders"
    run_folders = top_level_states["RunStepFolders"]
    assert run_folders["ItemsPath"] == "$.current_step_items"
    assert run_folders["ResultPath"] == "$.step_outcomes"
    assert run_folders["MaxConcurrency"] == 40
    assert top_level_states["CollectStepOutcomes"]["Parameters"] == {
        "collect_step_outcomes": True,
        "state.$": "$",
        "step_outcomes.$": "$.step_outcomes",
    }
    assert top_level_states["CollectStepOutcomes"]["Next"] == "AdvanceOrStop"
    assert top_level_states["AdvanceOrStop"]["Default"] == "NextStep"
    iterator = run_folders["Iterator"]
    states = iterator["States"]
    assert set(states) == {"RunFolder", "NormalizeFolderOutcome"}
    assert states["RunFolder"]["Next"] == "NormalizeFolderOutcome"
    assert states["RunFolder"]["Catch"][0]["Next"] == "NormalizeFolderOutcome"
    assert states["NormalizeFolderOutcome"]["Type"] == "Task"
    assert states["NormalizeFolderOutcome"]["Parameters"] == {
        "normalize_folder_outcome": True,
        "state.$": "$",
    }
    assert states["RunFolder"]["Parameters"]["StateMachineArn"].endswith(
        ":stateMachine:mock-read"
    )


@pytest.mark.parametrize(
    ("resource", "expected_child"),
    [
        ("openci_tf_apply", "mock-apply"),
        ("openci_tf_destroy", "mock-destroy"),
    ],
)
def test_rendered_mutation_graph_is_sequential_fail_fast_and_lane_locked(
    resource, expected_child
):
    definition = render_mutation_outer_definition(resource)
    top_level_states = definition["States"]
    assert top_level_states["NormalizeConfigError"]["Type"] == "Task"
    assert top_level_states["NormalizeConfigError"]["Parameters"] == {
        "normalize_config_error": True,
        "state.$": "$",
    }
    run_folders = top_level_states["RunFoldersSequential"]
    assert run_folders["MaxConcurrency"] == 1
    assert top_level_states["FailRunFolders"]["Result"] == {
        "failed_step": "RunFoldersSequential"
    }
    states = run_folders["Iterator"]["States"]
    assert "SequentialMergeFolderOutcome" not in states
    assert states["SequentialNormalizeFolderOutcome"]["Type"] == "Task"
    assert states["SequentialNormalizeFolderOutcome"]["Parameters"] == {
        "normalize_folder_outcome": True,
        "state.$": "$",
    }
    route = states["SequentialRouteChildOutcome"]
    assert route["Default"] == "SequentialFailFolderIteration"
    assert route["Choices"][0]["Next"] == "SequentialNormalizeFolderOutcome"
    assert states["SequentialRunFolder"]["Parameters"]["StateMachineArn"].endswith(
        f":stateMachine:{expected_child}"
    )
