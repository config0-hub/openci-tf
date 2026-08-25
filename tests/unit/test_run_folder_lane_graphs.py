# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural isolation checks for the three physical run-folder graphs."""
from __future__ import annotations

import pytest

from tests.helpers.asl_reachability import dangling_transitions, unreachable_states
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition
from src.domain.engine.outer_map_state import merge_map_item
from src.platform.aws.run_registry.step_index import registry_step_index_from_state


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


def test_collect_forwards_pipeline_step_index() -> None:
    read_collect = load_rendered_run_folder_definition("read")["States"]["Collect"][
        "Parameters"
    ]
    for lane in ("apply", "destroy"):
        mutation_collect = load_rendered_run_folder_definition(lane)["States"][
            "CollectMutation"
        ]["Parameters"]
        assert mutation_collect["step_index.$"] == "$.step_index"
    assert read_collect["step_index.$"] == "$.step_index"


def test_non_pipeline_run_folder_collect_input_includes_step_index() -> None:
    """Execution 1787690005089.c7ac9302: RunFolders omitted step_index from inner input."""
    map_shared = {
        "upstream_urls": {
            "infracost:0.10.39": "https://github.com/infracost/infracost/releases/download/v0.10.39/infracost-linux-amd64.tar.gz",
            "tfsec:1.28.10": "https://github.com/aquasecurity/tfsec/releases/download/v1.28.10/tfsec_1.28.10_linux_amd64.tar.gz",
            "tofu:1.8.0": "https://github.com/opentofu/opentofu/releases/download/v1.8.0/tofu_1.8.0_linux_amd64.tar.gz",
        },
        "repo_name": "williaumwu/openci-test-gitops",
        "git_url": "https://github.com/williaumwu/openci-test-gitops.git",
        "commit_hash": "e014dacb3293dc4a3d6f287855a92cb633020436",
        "ssm_openci_tf_github_token": "/openci-tf/clone-token/williaumwu-openci-test-gitops-control",
        "ssm_infracost_api_key": "",
    }
    compact_item = {
        "run_id": "1787690005089.c7ac9302",
        "folder": "terraform/primary/ap-northeast-1/06-sns-topic",
        "account_id": "998038917735",
        "action": "plan_destroy",
        "attempt": 0,
        "budget": 1040,
        "deadline_at": "2026-08-25T20:50:50Z",
        "step_index": 0,
        "b": [
            "openci-tf-executor-readonly",
            "openci-tf-executor-poweruser",
            "openci-tf-8e330376333ca0e7",
            3600,
        ],
        "c": {
            "version": 1,
            "timeout": 900,
            "tf_runtime": "tofu:1.8.0",
            "account_alias": "primary",
            "execution_target": "lambda",
            "extra_flags": [],
            "ssm_env_paths": [],
            "apply": {"allow": True, "grace_seconds": 15},
            "destroy": {"allow": True, "grace_seconds": 60},
        },
        "e": "1787690005089.c7ac9302.f445281ac67b.0",
    }
    inner_event = merge_map_item(map_shared, compact_item)
    collect_parameters = load_rendered_run_folder_definition("read")["States"]["Collect"][
        "Parameters"
    ]
    assert collect_parameters["step_index.$"] == "$.step_index"
    assert inner_event["step_index"] == 0
    assert registry_step_index_from_state(inner_event["step_index"]) == 1

    probe_complete_state = {
        **inner_event,
        "probe": {
            "exec_id": inner_event["execution_id"],
            "attempt": 0,
            "submitted_at": 1787690030.55072,
            "succeeded": True,
            "error": None,
            "credential_expired": False,
            "steps": [{"step_name": "step-0", "status": "succeeded", "exit_code": 0}],
            "pointers": {},
            "probe_status": "complete",
        },
    }
    step_index_path = collect_parameters["step_index.$"]
    assert step_index_path.startswith("$.")
    assert probe_complete_state[step_index_path.removeprefix("$.")] == 0


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
