"""Structural isolation checks for the three physical run-folder graphs."""
from __future__ import annotations

import pytest

from tests.helpers.asl_reachability import dangling_transitions, unreachable_states
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition


@pytest.mark.parametrize("lane", ["read", "apply", "destroy"])
def test_lane_graph_is_closed_and_reachable(lane: str) -> None:
    definition = load_rendered_run_folder_definition(lane)
    assert unreachable_states(definition) == []
    assert dangling_transitions(definition) == []
    for name, state in definition["States"].items():
        if state["Type"] == "Choice":
            assert "Default" in state, f"{lane}.{name} lacks a safe default"


def test_read_graph_contains_no_mutation_states() -> None:
    states = load_rendered_run_folder_definition("read")["States"]
    assert "Collect" in states
    assert "CollectMutation" not in states
    assert not [name for name in states if "Mutation" in name]
    assert not [name for name in states if "Safe" in name]


@pytest.mark.parametrize("lane", ["apply", "destroy"])
def test_mutation_graph_contains_no_read_collector_or_safe_variants(lane: str) -> None:
    states = load_rendered_run_folder_definition(lane)["States"]
    assert "CollectMutation" in states
    assert "Collect" not in states
    assert not [name for name in states if "Safe" in name]
    for state in states.values():
        assert "Collect" not in _transition_targets(state)


def test_each_graph_embeds_only_its_lane_actions() -> None:
    expected = {
        "read": {"plan", "plan_destroy", "drift", "report"},
        "apply": {"apply"},
        "destroy": {"destroy"},
    }
    for lane, actions in expected.items():
        choices = load_rendered_run_folder_definition(lane)["States"]["ValidateAction"][
            "Choices"
        ]
        assert {choice["StringEquals"] for choice in choices} == actions


def test_polling_retry_and_failure_routing_are_collapsed() -> None:
    for lane in ("read", "apply", "destroy"):
        states = load_rendered_run_folder_definition(lane)["States"]
        assert len(states) == 10
        assert states["ProbeDone"]["Next"] == "RouteProbeOutcome"
        assert states["WaitBeforeProbe"] == {
            "Type": "Wait",
            "Seconds": 30,
            "Next": "ProbeDone",
        }
        assert states["BookkeepCredentialRetry"]["Next"] == "PrepareAndSubmit"
        assert states["ValidateAction"]["Default"] == "WriteFailureManifest"
        assert states["WriteFailureManifest"]["Parameters"] == {
            "event.$": "$",
            "execution_started_at.$": "$$.Execution.StartTime",
        }
        assert all(
            catcher["Next"] in {"RouteProbeOutcome", "WriteFailureManifest"}
            for name in ("PrepareAndSubmit", "ProbeDone")
            for catcher in states[name]["Catch"]
        )
        removed = {
            "RejectUnsafeAction",
            "RouteProbeResult",
            "RetryOnCredentialExpiry",
            "NormalizePrepareFailure",
            "NormalizeProbeFailure",
            "NormalizeCollectFailure",
            "NormalizePrepareFailureMutation",
            "NormalizeProbeFailureMutation",
            "NormalizeCollectFailureMutation",
            "RouteCredentialRetryManifest",
            "RouteCredentialRetryExhausted",
            "RouteRetryIncrement",
            "IncrementAttemptSafe",
            "IncrementAttemptMutation",
            "WriteCredentialRetryManifestSafe",
            "WriteCredentialRetryManifestMutation",
        }
        assert removed.isdisjoint(states)


def test_every_probe_choice_value_check_has_a_presence_guard() -> None:
    for lane in ("read", "apply", "destroy"):
        route = load_rendered_run_folder_definition(lane)["States"][
            "RouteProbeOutcome"
        ]
        for rule in route["Choices"]:
            checks = rule.get("And", [rule])
            probe_value_paths = {
                check["Variable"]
                for check in checks
                if check.get("Variable", "").startswith("$.probe.")
                and "IsPresent" not in check
            }
            present_paths = {
                check["Variable"]
                for check in checks
                if check.get("IsPresent") is True
            }
            assert probe_value_paths <= present_paths


def _transition_targets(state: dict) -> set[str]:
    targets: set[str] = set()
    if "Next" in state:
        targets.add(state["Next"])
    if state.get("Type") == "Choice":
        targets.update(choice["Next"] for choice in state.get("Choices", []))
        targets.add(state["Default"])
    for catcher in state.get("Catch", []):
        targets.add(catcher["Next"])
    return targets
