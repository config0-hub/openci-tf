# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Isolated lane architecture tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

READ_OUTER = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text()
APPLY_OUTER = Path(
    "infra/deploy/modules/openci_tf/step_function_mutation_outer.tf"
).read_text()
READ_INNER = Path("infra/deploy/modules/run_folder/step_function.tf").read_text()
READ_IAM = Path("infra/deploy/modules/openci_tf/iam.tf").read_text()
RUN_FOLDER_IAM = Path("infra/deploy/modules/run_folder/iam.tf").read_text()
DEPLOY_MAIN = Path("infra/deploy/main.tf").read_text()


def _state_block(source: str, name: str) -> str:
    return source.split(f"      {name} = {{", 1)[1].split("\n      }", 1)[0]


def test_three_outer_state_machine_resources_exist():
    assert 'resource "aws_sfn_state_machine" "openci_tf"' in READ_OUTER
    assert 'resource "aws_sfn_state_machine" "openci_tf_apply"' in APPLY_OUTER
    assert 'resource "aws_sfn_state_machine" "openci_tf_destroy"' in APPLY_OUTER
    assert 'name     = "${var.project_name}-apply"' in APPLY_OUTER
    assert 'name     = "${var.project_name}-destroy"' in APPLY_OUTER


def test_read_outer_rejects_mutation_confirm_routes():
    route = _state_block(READ_OUTER, "RouteAction")
    assert "ConfirmApplyIntent" not in route
    assert "ConfirmDestroyIntent" not in route
    assert "CreateIntent" in READ_OUTER
    assert "CreateApplyIntent" not in READ_OUTER
    assert "CreateDestroyIntent" not in READ_OUTER
    assert "RunFoldersSequential" not in READ_OUTER


def test_apply_outer_accepts_only_apply_confirm():
    route = _state_block(APPLY_OUTER, "RouteAction")
    assert 'StringEquals = "apply"' in route
    assert "intent_confirm" in route
    assert "destroy" not in route.lower().split("routeaction")[1].split("default")[0]


def test_destroy_outer_accepts_only_destroy_confirm():
    route = _state_block(
        APPLY_OUTER.split('resource "aws_sfn_state_machine" "openci_tf_destroy"')[1],
        "RouteAction",
    )
    assert 'StringEquals = "destroy"' in route
    assert "intent_confirm" in route


def test_inner_lane_allowed_actions_are_isolated():
    assert "for action in local.allowed_actions" in READ_INNER
    assert "lane" in Path("infra/deploy/modules/run_folder/variables.tf").read_text()
    assert re.search(r'lane\s*=\s*"apply"', DEPLOY_MAIN)
    assert re.search(r'lane\s*=\s*"destroy"', DEPLOY_MAIN)


def test_mutation_inner_uses_codebuild_not_engine_lambda():
    assert (
        "ENGINE_CODEBUILD_STATE_MACHINE_ARN"
        in Path("infra/deploy/modules/run_folder/lambdas.tf").read_text()
    )
    run_folder_main = Path("infra/deploy/modules/run_folder/main.tf").read_text()
    assert "prepare_engine_submit_statements" in run_folder_main
    assert "states:StartExecution" in run_folder_main
    assert "lambda:InvokeFunction" in RUN_FOLDER_IAM


def test_distinct_inner_arns_wired_to_mutation_outers():
    assert "var.run_folder_apply_state_machine_arn" in APPLY_OUTER
    assert "var.run_folder_destroy_state_machine_arn" in APPLY_OUTER
    assert "run_folder_apply_state_machine_arn" in DEPLOY_MAIN
    assert "run_folder_destroy_state_machine_arn" in DEPLOY_MAIN


def test_read_outer_iam_cannot_start_mutation_inners():
    read_policy = READ_IAM.split('resource "aws_iam_role_policy" "stepfunction"')[1]
    assert "var.run_folder_state_machine_arn" in read_policy
    assert "run_folder_apply_state_machine_arn" not in read_policy
    assert "run_folder_destroy_state_machine_arn" not in read_policy


def test_lambda_env_threads_three_outer_arns():
    lambdas = Path("infra/deploy/modules/openci_tf/lambdas.tf").read_text()
    assert "APPLY_STEP_FUNCTION_ARN" in lambdas
    assert "DESTROY_STEP_FUNCTION_ARN" in lambdas
    assert "STEP_FUNCTION_ARN" in lambdas


def test_mutation_outer_sequential_fail_fast():
    apply_seq = _state_block(APPLY_OUTER, "RunFoldersSequential")
    assert "MaxConcurrency = 1" in apply_seq
    assert "SequentialFailFolderIteration" in apply_seq


def test_engine_payload_fields_unchanged():
    import base64

    from src.domain.engine.payload import EnginePayload

    payload = EnginePayload(
        "id",
        "s3://bucket/package",
        "kms",
        "",
        base64.b64encode(b'["bash ./openci_tf_run.sh"]').decode(),
        "s3://bucket/done",
        "codebuild",
        900,
    )
    payload.validate()
    assert set(payload.__dict__) == {
        "trigger_id",
        "s3_package_uri",
        "sops_type",
        "sops_path",
        "commands_b64",
        "done_endpoint",
        "execution_target",
        "timeout_seconds",
    }


def test_prepare_lane_mode_rejects_cross_verb(monkeypatch):
    from src.services.run_folder import prepare_and_submit as mod

    monkeypatch.setenv("LANE_MODE", "apply")
    with pytest.raises(ValueError, match="apply lane rejects action"):
        mod._validate_lane_action("destroy", "apply")
    monkeypatch.setenv("LANE_MODE", "read")
    with pytest.raises(ValueError, match="read lane rejects mutation"):
        mod._validate_lane_action("apply", "read")


def test_start_run_routes_confirmed_apply_to_apply_outer(monkeypatch):
    from src.services.orchestration.start_run import _step_function_arn_for_request
    from src.services.webhook.run_request import github_run_request

    monkeypatch.setenv("STEP_FUNCTION_ARN", "arn:read")
    monkeypatch.setenv("APPLY_STEP_FUNCTION_ARN", "arn:apply")
    monkeypatch.setenv("DESTROY_STEP_FUNCTION_ARN", "arn:destroy")
    request = github_run_request(
        {"trigger_id": "t", "commit_hash": "a" * 40, "pr_number": 1},
        action="apply",
        folders=[],
        all_flag=False,
        affected_flag=False,
        delivery_id="641353f2-760d-4f70-82e9-e85611860c00",
        confirm_token="tok",
        intent_confirm=True,
    )
    assert _step_function_arn_for_request(request) == "arn:apply"


@pytest.mark.parametrize("source", [READ_OUTER, APPLY_OUTER])
def test_choice_presence_guards(source):
    for match in re.finditer(
        r'\{ Variable = "(\$\.[A-Za-z_.]+)", BooleanEquals', source
    ):
        variable = match.group(1)
        if variable == "$.action":
            continue
        window = source[max(0, match.start() - 400) : match.start()]
        assert f'Variable = "{variable}", IsPresent = true' in window


def test_mutation_prepare_iam_includes_codebuild_lookup():
    iam = Path("infra/deploy/modules/run_folder/iam.tf").read_text()
    main = Path("infra/deploy/modules/run_folder/main.tf").read_text()
    assert "codebuild:BatchGetBuilds" in iam
    assert "codebuild:ListBuildsForProject" in main
    assert "engine_codebuild_project_arn" in main


def test_mutation_inner_probe_and_collect_keep_build_id_optional():
    from tests.helpers.rendered_run_folder_asl import (
        load_rendered_run_folder_definition,
    )

    states = load_rendered_run_folder_definition("apply")["States"]
    assert "Parameters" not in states["ProbeDone"]
    assert "codebuild_build_id.$" not in states["CollectMutation"]["Parameters"]


def test_read_inner_rendered_poll_does_not_require_codebuild_build_id():
    from tests.helpers.rendered_run_folder_asl import (
        load_rendered_run_folder_definition,
    )

    definition = load_rendered_run_folder_definition()
    probe = definition["States"]["ProbeDone"]
    assert "Parameters" not in probe
    assert probe["ResultPath"] == "$.probe"


def test_read_lane_render_parameters_do_not_reference_intent_only_keys():
    # Read-only executions are started from webhook/API run requests, which never
    # carry intent comment metadata. A JSONPath Parameters entry for a missing key
    # raises States.Runtime, and the RenderPR Catch would swallow it, so no PR
    # comment would be posted. Only the mutation outer machine may reference these.
    for key in (
        "requested_comment_id",
        "requested_comment_body",
        "intent_comment_id",
        "consumed_confirm_token",
    ):
        assert f'"{key}.$"' not in READ_OUTER, key
        assert f'"{key}.$"' in APPLY_OUTER, key
