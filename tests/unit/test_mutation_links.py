# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation outer ARN and real CodeBuild build ID comment wiring tests."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlparse
from unittest.mock import MagicMock

import pytest  # type: ignore[import-not-found]

from src.core.errors import EngineAckError
from src.domain.formatters.console_urls import (
    codebuild_build_url,
    is_valid_codebuild_build_id,
    s3_object_console_url,
    step_functions_execution_url,
)
from src.domain.engine.artifact_paths import artifact_env_suffix
from src.platform.aws import engine
from src.services.orchestration.start_run import _step_function_arn_for_request
from src.services.render.handler import (
    _mutation_codebuild_url,
    _mutation_folder_comment,
    _mutation_grace_seconds,
)
from src.services.webhook.run_request import github_run_request
from tests.helpers.frozen_account import MAIN_ACCOUNT_ID


def test_mutation_plan_show_artifact_uses_valid_environment_suffix():
    assert artifact_env_suffix("plan-show.out") == "PLAN_SHOW_OUT"


def test_start_codebuild_execution_never_fabricates_build_id(monkeypatch):
    sfn = MagicMock()
    sfn.start_execution.return_value = {
        "executionArn": "arn:aws:states:us-east-1:123456789012:execution:engine-codebuild:exec",
    }
    monkeypatch.setenv("ENGINE_CODEBUILD_PROJECT_NAME", "openci-tf-worker")
    monkeypatch.setattr(
        engine.boto3,
        "client",
        lambda name: sfn if name == "stepfunctions" else MagicMock(),
    )
    monkeypatch.setattr(
        engine, "resolve_codebuild_build_id", lambda *_args, **_kwargs: None
    )

    ack = engine.start_codebuild_execution(
        "arn:aws:states:us-east-1:123456789012:stateMachine:engine-codebuild",
        {
            "trigger_id": "run.infra.0",
            "timeout_seconds": 900,
            "s3_package_uri": "s3://bucket/pkg",
            "commands_b64": "YQ==",
            "done_endpoint": "s3://bucket/done",
            "execution_target": "codebuild",
        },
    )

    assert "codebuild_build_id" not in ack
    assert ack["engine_execution_arn"].endswith(":exec")
    sfn_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
    assert sfn_input["build_timeout_minutes"] == 18
    assert sfn_input["sfn_timeout_seconds"] == 1500
    assert sfn_input["timeout_seconds"] == "900"
    assert sfn_input["callback_url"] == ""


def test_start_codebuild_execution_rejects_missing_timeout(monkeypatch):
    monkeypatch.setattr(engine.boto3, "client", lambda name: MagicMock())
    with pytest.raises(EngineAckError, match="timeout_seconds"):
        engine.start_codebuild_execution(
            "arn:aws:states:us-east-1:123456789012:stateMachine:engine-codebuild",
            {"trigger_id": "run.infra.0"},
        )


def test_resolve_codebuild_build_id_matches_trigger_env(monkeypatch):
    codebuild = MagicMock()
    codebuild.list_builds_for_project.return_value = {
        "ids": ["openci-tf-worker:11111111-2222-3333-4444-555555555555"],
    }
    codebuild.batch_get_builds.return_value = {
        "builds": [
            {
                "id": "openci-tf-worker:11111111-2222-3333-4444-555555555555",
                "environment": {
                    "environmentVariables": [
                        {"name": "TRIGGER_ID", "value": "run.infra.0"},
                    ]
                },
            }
        ]
    }
    monkeypatch.setattr(
        engine.boto3,
        "client",
        lambda name: codebuild if name == "codebuild" else MagicMock(),
    )

    build_id = engine.resolve_codebuild_build_id(
        "openci-tf-worker", "run.infra.0", max_attempts=1, sleep_seconds=0
    )

    assert build_id == "openci-tf-worker:11111111-2222-3333-4444-555555555555"


def test_resolve_codebuild_build_id_paginates(monkeypatch):
    codebuild = MagicMock()
    codebuild.list_builds_for_project.side_effect = [
        {
            "ids": ["openci-tf-worker:00000000-0000-0000-0000-000000000001"],
            "nextToken": "page-2",
        },
        {
            "ids": ["openci-tf-worker:11111111-2222-3333-4444-555555555555"],
        },
    ]
    codebuild.batch_get_builds.side_effect = [
        {
            "builds": [
                {
                    "id": "openci-tf-worker:00000000-0000-0000-0000-000000000001",
                    "environment": {"environmentVariables": []},
                }
            ]
        },
        {
            "builds": [
                {
                    "id": "openci-tf-worker:11111111-2222-3333-4444-555555555555",
                    "environment": {
                        "environmentVariables": [
                            {"name": "TRIGGER_ID", "value": "run.infra.0"}
                        ],
                    },
                }
            ]
        },
    ]
    monkeypatch.setattr(
        engine.boto3,
        "client",
        lambda name: codebuild if name == "codebuild" else MagicMock(),
    )

    build_id = engine.resolve_codebuild_build_id(
        "openci-tf-worker", "run.infra.0", max_attempts=1, sleep_seconds=0, max_pages=3
    )

    assert build_id == "openci-tf-worker:11111111-2222-3333-4444-555555555555"
    assert codebuild.list_builds_for_project.call_count == 2


def test_codebuild_url_rejects_step_functions_execution_arn():
    inner_arn = "arn:aws:states:us-east-1:123456789012:execution:openci-tf-run-folder-apply:folder-run"
    assert is_valid_codebuild_build_id(inner_arn) is False
    with pytest.raises(ValueError, match="build_id"):
        codebuild_build_url("openci-tf-worker", inner_arn, region="us-east-1")


def test_codebuild_url_rejects_fabricated_trigger_suffix():
    fake_id = "openci-tf-worker:run.infra.0"
    assert is_valid_codebuild_build_id(fake_id) is False


def test_s3_object_console_url_can_wrap_destination_in_identity_center_shortcut():
    url = s3_object_console_url(
        "tmp-bucket",
        "openci-tf/org/repo/run/infra/a/tfsec.json",
        region="us-east-1",
        account_id=MAIN_ACCOUNT_ID,
        identity_center_start_url="https://d-9567aa6b98.awsapps.com/start/#",
        identity_center_role_name="AWSAdministratorAccess",
    )

    parsed = urlparse(url)
    fragment_query = parsed.fragment.split("?", 1)[1]
    query = parse_qs(fragment_query)
    assert url.startswith("https://d-9567aa6b98.awsapps.com/start/#/console?")
    assert query["account_id"] == [MAIN_ACCOUNT_ID]
    destination = unquote(query["destination"][0])
    assert destination.startswith("https://us-east-1.console.aws.amazon.com/s3/object/tmp-bucket")
    assert "openci-tf/org/repo/run/infra/a/tfsec.json" in destination


def test_codebuild_url_can_wrap_destination_in_identity_center_shortcut():
    url = codebuild_build_url(
        "openci-tf-worker",
        "openci-tf-worker:11111111-2222-3333-4444-555555555555",
        region="us-east-1",
        account_id=MAIN_ACCOUNT_ID,
        identity_center_start_url="https://d-9567aa6b98.awsapps.com/start/#",
        identity_center_role_name="AWSAdministratorAccess",
    )

    parsed = urlparse(url)
    fragment_query = parsed.fragment.split("?", 1)[1]
    query = parse_qs(fragment_query)
    assert url.startswith("https://d-9567aa6b98.awsapps.com/start/#/console?")
    assert query["account_id"] == [MAIN_ACCOUNT_ID]
    assert query["role_name"] == ["AWSAdministratorAccess"]
    destination = unquote(query["destination"][0])
    assert destination.startswith(
        "https://us-east-1.console.aws.amazon.com/codesuite/codebuild/"
    )
    assert "openci-tf-worker:11111111-2222-3333-4444-555555555555" in destination


def test_start_run_routes_read_plan_to_read_outer(monkeypatch):
    monkeypatch.setenv("STEP_FUNCTION_ARN", "arn:read")
    monkeypatch.setenv("APPLY_STEP_FUNCTION_ARN", "arn:apply")
    monkeypatch.setenv("DESTROY_STEP_FUNCTION_ARN", "arn:destroy")
    request = github_run_request(
        {"trigger_id": "t", "commit_hash": "a" * 40, "pr_number": 1},
        action="plan",
        folders=[],
        all_flag=False,
        affected_flag=False,
        delivery_id="641353f2-760d-4f70-82e9-e85611860c00",
    )
    assert _step_function_arn_for_request(request) == "arn:read"


def test_mutation_grace_defaults_without_or_zero_fallback():
    assert _mutation_grace_seconds({}, "apply") == 15
    assert _mutation_grace_seconds({}, "destroy") == 60
    assert _mutation_grace_seconds({"grace_seconds": 25}, "apply") == 25


def test_mutation_codebuild_url_omits_invalid_build_id():
    assert (
        _mutation_codebuild_url({"codebuild_build_id": "openci-tf-worker:not-a-uuid"})
        is None
    )


def test_mutation_codebuild_url_uses_identity_center_account_shortcut(monkeypatch):
    monkeypatch.setenv("ENGINE_CODEBUILD_PROJECT_NAME", "openci-tf-worker")
    monkeypatch.setenv("ENGINE_CODEBUILD_ACCOUNT_ID", MAIN_ACCOUNT_ID)
    monkeypatch.setenv(
        "AWS_CONSOLE_START_URL", "https://d-9567aa6b98.awsapps.com/start"
    )
    monkeypatch.setenv("AWS_CONSOLE_ROLE_NAME", "AWSAdministratorAccess")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    url = _mutation_codebuild_url(
        {"codebuild_build_id": "openci-tf-worker:11111111-2222-3333-4444-555555555555"}
    )

    assert url is not None
    parsed = urlparse(url)
    fragment_query = parsed.fragment.split("?", 1)[1]
    query = parse_qs(fragment_query)
    assert parsed.netloc == "d-9567aa6b98.awsapps.com"
    assert query["account_id"] == [MAIN_ACCOUNT_ID]
    assert query["role_name"] == ["AWSAdministratorAccess"]
    assert "codesuite/codebuild" in unquote(query["destination"][0])


def test_mutation_infrastructure_error_renders_failed_not_succeeded(monkeypatch):
    monkeypatch.delenv("ENGINE_CODEBUILD_PROJECT_NAME", raising=False)
    body = _mutation_folder_comment(
        "config",
        {
            "folder": "config",
            "status": "infrastructure_error",
            "error": "configuration resolution failed",
        },
        {},
        action="apply",
        commit_hash="a" * 40,
        console_url="https://console.example/execution",
        run_id="run",
        repo_name="org/repo",
        pr_number=7,
    )

    assert "config · Apply ❌ failed" in body
    assert "Apply succeeded" not in body
    assert "configuration resolution failed" in body


def test_outer_execution_url_differs_from_inner_execution_url():
    outer = "arn:aws:states:us-east-1:123456789012:execution:openci-tf-apply:run"
    inner = (
        "arn:aws:states:us-east-1:123456789012:execution:openci-tf-run-folder-apply:folder"
    )
    outer_url = step_functions_execution_url(outer, region="us-east-1")
    inner_url = step_functions_execution_url(inner, region="us-east-1")
    assert outer_url != inner_url
    assert "openci-tf-apply" in outer_url
    assert "openci-tf-run-folder-apply" in inner_url
