# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2 orchestration safety and rendered-definition coverage."""
import re
from pathlib import Path

import pytest

from src.core.models import FolderConfig
from src.domain.cmd_builder.cmd_resolver import resolve_commands
from src.services.render import handler as render_handler

SOURCE = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text()
RUN_FOLDER_SOURCE = Path("infra/deploy/modules/run_folder/step_function.tf").read_text()
EXECUTION_EVENTS_SOURCE = Path("infra/deploy/modules/openci_tf/execution_events.tf").read_text()


def _state_block(source: str, name: str) -> str:
    return source.split(f"      {name} = {{", 1)[1].split("\n      }", 1)[0]


def test_outer_definition_has_safe_and_intent_lanes():
    for verb in ("plan", "plan_destroy", "drift", "report"):
        assert f'StringEquals = "{verb}", Next = "ValidateAndResolve"' in SOURCE
    assert "CreateIntent" in SOURCE
    assert "CreateApplyIntent" not in SOURCE
    assert "CreateDestroyIntent" not in SOURCE
    assert "IntentFailed" in SOURCE
    assert "MaxConcurrency = var.run_folder_max_concurrency" in SOURCE
    MUTATION_OUTER = Path("infra/deploy/modules/openci_tf/step_function_mutation_outer.tf").read_text()
    assert "MaxConcurrency = 1" in MUTATION_OUTER
    assert "states:startExecution.sync:2" in SOURCE
    assert "NormalizeFolderOutcome" in SOURCE
    assert "NormalizeStepFolderOutcome" in SOURCE
    assert 'ErrorEquals = ["States.ALL"]' in SOURCE
    assert 'normalize_folder_outcome = true' in SOURCE
    assert "RouteChildOutcome" not in SOURCE
    assert "MergeFolderOutcome" not in SOURCE
    assert "NormalizeMalformedChildOutcome" not in SOURCE
    assert "States.StringToJson($.Output)" not in SOURCE
    assert '"account_id.$"' in SOURCE and "$$.Map.Item.Value.account_id" in SOURCE
    assert '"folder.$"' in SOURCE and "$$.Map.Item.Value.folder" in SOURCE
    assert '"account_id.$" = "$.Input.account_id"' not in SOURCE


def test_mutation_item_selectors_forward_pipeline_step_index():
    mutation_source = Path("infra/deploy/modules/openci_tf/step_function_mutation_outer.tf").read_text()
    assert mutation_source.count('"step_index.$"                 = "$.step_index"') == 2


def test_run_folders_item_selector_forwards_map_item_step_index():
    block = _state_block(SOURCE, "RunFolders")
    assert '"step_index.$"                 = "$$.Map.Item.Value.step_index"' in block
    assert '"step_index.$"                 = "$.step_index"' not in block


def test_deployed_state_machines_route_safe_and_intent_verbs():
    outer_routes = re.findall(r'StringEquals = "(\w+)", Next = "(\w+)"', _state_block(SOURCE, "RouteAction"))
    assert {verb for verb, next_state in outer_routes if next_state == "ValidateAndResolve"} == {
        "plan", "plan_destroy", "drift", "report",
    }
    assert ("validate", "ValidateAndResolve") not in outer_routes
    assert _state_block(SOURCE, "RouteAction").count('Next = "CreateIntent"') == 2
    mutation_source = Path("infra/deploy/modules/openci_tf/step_function_mutation_outer.tf").read_text()
    assert "ConfirmDestroyIntent" in mutation_source
    assert "ConfirmApplyIntent" in mutation_source


def test_no_op_uses_the_normal_empty_map_and_final_render_path():
    assert "RouteResolved" not in SOURCE
    assert "RenderNoOp" not in SOURCE
    assert "FailRenderNoOp" not in SOURCE
    assert 'Next = "RenderPlaceholder"' in _state_block(SOURCE, "ValidateAndResolve")
    assert 'ItemsPath      = "$.current_step_items"' in _state_block(SOURCE, "RunStepFolders")
    assert '"no_op_reason.$"        = "$.no_op_reason"' in _state_block(
        SOURCE, "RenderPR"
    )


def test_uncatchable_outer_failures_have_registry_finalization_backstop():
    for terminal_status in ("FAILED", "TIMED_OUT", "ABORTED"):
        assert f'"{terminal_status}"' in EXECUTION_EVENTS_SOURCE
    assert "aws_sfn_state_machine.openci_tf.arn" in EXECUTION_EVENTS_SOURCE
    assert 'aws_lambda_function.functions["finalize-run"].arn' in EXECUTION_EVENTS_SOURCE
    assert 'principal     = "events.amazonaws.com"' in EXECUTION_EVENTS_SOURCE


def test_render_pr_failure_finalizes_then_fails_instead_of_done():
    render_pr = _state_block(SOURCE, "RenderPR")
    assert 'Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizeAfterRenderFailure" }]' in render_pr
    assert 'Next       = "RouteAfterRender"' in render_pr
    route_after_render = _state_block(SOURCE, "RouteAfterRender")
    assert 'Next = "FinalizeRun"' in route_after_render
    assert 'render_flags.execution_failed' in route_after_render
    assert 'Default = "Done"' in route_after_render
    finalize_after_render = _state_block(SOURCE, "FinalizeAfterRenderFailure")
    assert 'local.lambda_arns["finalize-run"]' in finalize_after_render
    assert 'Next       = "RenderPRFailed"' in finalize_after_render
    render_failed = _state_block(SOURCE, "RenderPRFailed")
    assert 'Type  = "Fail"' in render_failed
    assert 'Error = "RenderPRFailed"' in render_failed
    finalize_run = _state_block(SOURCE, "FinalizeRun")
    assert 'Next       = "PipelineFailed"' in finalize_run
    assert "RouteAfterFinalize" not in SOURCE
    assert "ConfigResolutionFailed" not in SOURCE
    pipeline_failed = _state_block(SOURCE, "PipelineFailed")
    assert 'Type  = "Fail"' in pipeline_failed
    assert 'Error = "PipelineFailed"' in pipeline_failed
    render_pipeline_failure = _state_block(SOURCE, "RenderPipelineFailure")
    assert 'local.lambda_arns["render-pr"]' in render_pipeline_failure
    assert 'Next       = "FinalizeRun"' in render_pipeline_failure
    assert 'Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizeRun" }]' in render_pipeline_failure


def test_pipeline_failure_catch_chains_end_in_fail():
    for state in (
        "ParseCommand",
        "CreateIntent",
        "ValidateAndResolve",
        "RunStepFolders",
        "CollectStepOutcomes",
    ):
        block = _state_block(SOURCE, state)
        assert "RenderPipelineFailure" in block or "Fail" in block
    assert "FailParseCommand" in SOURCE
    assert "PipelineFailed" in SOURCE


def test_validate_and_resolve_config_errors_are_normalized_for_pr_feedback():
    assert "RenderEarlyPlaceholder" not in SOURCE
    assert "execution_arn.$" in SOURCE and "$$.Execution.Id" in SOURCE
    assert "ValidateAndResolve" in SOURCE
    assert "NormalizeConfigError" in SOURCE
    normalize_config = _state_block(SOURCE, "NormalizeConfigError")
    assert 'Type     = "Task"' in normalize_config
    assert 'local.lambda_arns["render-pr"]' in normalize_config
    assert "normalize_config_error = true" in normalize_config
    assert '"state.$"              = "$"' in normalize_config
    assert "RenderPR" in SOURCE
    render_pr = _state_block(SOURCE, "RenderPR")
    assert 'ResultPath = "$.render_flags"' in render_pr
    route_after_render = _state_block(SOURCE, "RouteAfterRender")
    assert 'Next = "FinalizeRun"' in route_after_render
    assert 'render_flags.execution_failed' in route_after_render
    assert 'Default = "Done"' in route_after_render
    finalize_run = _state_block(SOURCE, "FinalizeRun")
    assert 'Next       = "PipelineFailed"' in finalize_run
    assert "ConfigResolutionFailed" not in SOURCE
    assert "NormalizeMapFailure" not in SOURCE


@pytest.mark.parametrize("action", ["apply", "destroy", "plan_destroy"])
def test_mutation_actions_resolve_to_commands(action):
    resolved = resolve_commands(action, FolderConfig(account_alias="target", apply=True, destroy=True))
    assert resolved.verb == action


def test_every_boolean_choice_variable_is_presence_guarded():
    """States.Runtime protection: a Choice BooleanEquals on an optional field
    must be paired with an IsPresent guard in the same And block (live failure
    4f1e7077: '$.intent_create' referenced an invalid value)."""
    import re

    for match in re.finditer(r'\{ Variable = "(\$\.[A-Za-z_.]+)", BooleanEquals', SOURCE):
        variable = match.group(1)
        if variable == "$.action":
            continue
        window = SOURCE[max(0, match.start() - 400): match.start()]
        assert (
            f'Variable = "{variable}", IsPresent = true' in window
        ), f"BooleanEquals on {variable} lacks a preceding IsPresent guard"


@pytest.mark.parametrize(
    ("child_execution", "expected"),
    [
        (
            {"Output": {"exec_id": "run.folder.0", "succeeded": True}},
            {
                "folder": "infra/a",
                "account_id": "123456789012",
                "execution_id": "run.folder.0",
                "output": {"exec_id": "run.folder.0", "succeeded": True},
            },
        ),
        (
            {"Output": {"succeeded": True}},
            {
                "folder": "infra/a",
                "account_id": "123456789012",
                "execution_id": "outer.folder.0",
                "attempt": 0,
                "status": "infrastructure_error",
                "succeeded": False,
                "error": "malformed child execution output",
            },
        ),
        (
            None,
            {
                "folder": "infra/a",
                "account_id": "123456789012",
                "execution_id": "outer.folder.0",
                "attempt": 0,
                "status": "infrastructure_error",
                "succeeded": False,
                "error": "nested execution failed",
            },
        ),
    ],
)
def test_render_consumer_normalizes_child_execution_envelopes(child_execution, expected):
    state = {
        "folder": "infra/a",
        "account_id": "123456789012",
        "execution_id": "outer.folder.0",
        "attempt": 0,
    }
    if child_execution is not None:
        state["child_execution"] = child_execution
    assert render_handler.handler(
        {"normalize_folder_outcome": True, "state": state}, object()
    ) == expected


def _render_pr_state_input_keys() -> set[str]:
    render_pr = _state_block(SOURCE, "RenderPR")
    parameters = render_pr.split("Parameters = {", 1)[1].split("\n        }", 1)[0]
    return {
        match.group(1)
        for match in re.finditer(r'"([A-Za-z0-9_]+)\.\$"\s*=\s*"\$\.([A-Za-z0-9_]+)"', parameters)
        if match.group(1) == match.group(2)
    } - {"execution_arn"}


def test_normalize_config_resolution_error_includes_render_pr_state_keys():
    state = {
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7},
        "settings": {"ssm_openci_tf_github_token": "/openci-tf/github-token"},
        "run_id": "outer-run",
        "notification_target": {"type": "github_pr"},
        "action": "plan",
        "deadline_at": "2999-01-01T00:00:00Z",
    }
    normalized = render_handler.handler(
        {"normalize_config_error": True, "state": state}, object()
    )
    assert _render_pr_state_input_keys() <= set(normalized)


def test_render_consumer_normalizes_config_resolution_error():
    state = {
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7},
        "settings": {"ssm_openci_tf_github_token": "/openci-tf/github-token"},
        "run_id": "outer-run",
        "notification_target": {"type": "github_pr"},
        "action": "plan",
    }
    assert render_handler.handler(
        {"normalize_config_error": True, "state": state}, object()
    ) == {
        **state,
        "deadline_at": None,
        "config_resolution_failed": True,
        "steps": [],
        "step_index": 0,
        "step_count": 0,
        "outcomes": [
            {
                "folder": "config",
                "status": "infrastructure_error",
                "error": "configuration resolution failed",
            }
        ],
        "skipped": [],
        "no_op_reason": None,
        "folders": [],
        "all_flag": False,
        "affected_flag": False,
        "requested_comment_id": None,
        "requested_comment_body": None,
        "intent_comment_id": None,
        "consumed_confirm_token": None,
        "confirm_token": None,
    }


def test_merged_intent_failure_comment_keeps_routed_action(monkeypatch):
    bodies: list[str] = []
    monkeypatch.setattr(render_handler, "get_github_token", lambda _path: "token")
    monkeypatch.setattr(render_handler, "GitHubClient", lambda _token: object())
    monkeypatch.setattr(
        render_handler,
        "_delete_and_repost_unmanaged",
        lambda _client, _repo, _pr, body, _kind: bodies.append(body),
    )
    monkeypatch.setattr(
        render_handler, "_delete_transient_status_comment", lambda *_args: None
    )

    render_handler.handler(
        {
            "pipeline_failure": {
                "failed_step": "CreateIntent",
                "action": "destroy",
            },
            "webhook_info": {"repo_name": "org/repo", "pr_number": 7},
            "settings": {"ssm_openci_tf_github_token": "/openci-tf/github-token"},
            "run_id": "outer-run",
            "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:openci-tf:run",
        },
        object(),
    )

    assert len(bodies) == 1
    assert "failed at CreateIntent (destroy)" in bodies[0]


def test_render_pipeline_failure_preserves_state_for_finalize():
    """Live bug: RenderPipelineFailure without ResultPath replaced the state, so
    FinalizeRun lost map_items/outcomes and never released folder locks."""
    block = _state_block(SOURCE, "RenderPipelineFailure")
    assert "ResultPath = null" in block


def test_failure_marker_pass_states_preserve_state():
    """Every Fail* Pass state must inject pipeline_failure via ResultPath and
    never rebuild a minimal state (live bug: FinalizeRun lost map_items and
    left folder locks held after mutation failures)."""
    import re

    for match in re.finditer(r'(Fail[A-Za-z]+) = \{\n        [\s\S]*?\n      \}', SOURCE):
        block = match.group(0)
        if 'Type       = "Pass"' in block or 'Type = "Pass"' in block:
            assert 'ResultPath = "$.pipeline_failure"' in block, match.group(1)
            assert '"webhook_info.$"' not in block, match.group(1)
