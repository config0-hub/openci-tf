# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authoritative submission acknowledgement and terminal evidence redaction."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from src.core.models import FolderConfig
from src.core.terminal_evidence import (
    MAX_TERMINAL_EVIDENCE_FIELDS,
    MAX_TERMINAL_EVIDENCE_ITEMS,
    MAX_TERMINAL_EVIDENCE_TEXT_CHARS,
    MAX_TERMINAL_EVIDENCE_TEXT_JSON_BYTES,
    redact_and_bound_terminal_evidence,
)
from src.domain.engine import manifest, result as engine_result, summary as engine_summary
from src.domain.engine.result import parse_result
from src.domain.formatters import artifacts as artifact_formatters
from src.domain.formatters.artifacts import folder_comment
from src.services.render.handler import _render_pipeline_failure
from src.services.run_folder import prepare_and_submit, write_failure_manifest
from src.services.run_folder import collect, poll_done
from src.services.run_folder import notify as run_folder_notify
from src.services.render import handler as render_handler
from src.platform.aws import run_registry


def _assert_bounded(value: object) -> None:
    if isinstance(value, str):
        assert len(value) <= MAX_TERMINAL_EVIDENCE_TEXT_CHARS
        assert (
            len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
            <= MAX_TERMINAL_EVIDENCE_TEXT_JSON_BYTES
        )
    elif isinstance(value, dict):
        assert len(value) <= MAX_TERMINAL_EVIDENCE_FIELDS
        for key, item in value.items():
            assert len(key) <= 64
            _assert_bounded(item)
    elif isinstance(value, list):
        assert len(value) <= MAX_TERMINAL_EVIDENCE_ITEMS
        for item in value:
            _assert_bounded(item)


def test_redactor_scrubs_secrets_and_bounds_every_field_and_collection() -> None:
    access_key = "AKIA1234567890ABCDEF"
    github_token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    raw = {
        f"field-{index}-token=key-secret": [
            {
                "error": (
                    f"AWS_ACCESS_KEY_ID={access_key} "
                    "AWS_SECRET_ACCESS_KEY=secret-value "
                    "Authorization: Bearer bearer-value "
                    f"password=hunter2 {github_token} "
                    + ("\U0001f680" * 500)
                )
            }
            for _ in range(24)
        ]
        for index in range(24)
    }

    bounded = redact_and_bound_terminal_evidence(raw)

    assert isinstance(bounded, dict)
    encoded = json.dumps(bounded)
    for secret in (
        access_key,
        "secret-value",
        "bearer-value",
        "hunter2",
        github_token,
        "key-secret",
    ):
        assert secret not in encoded
    assert "***" in encoded
    _assert_bounded(bounded)
    numeric_bounds = redact_and_bound_terminal_evidence(
        {"non_finite": float("inf"), "huge_integer": 2**100}
    )
    assert numeric_bounds == {"non_finite": "...", "huge_integer": "..."}


def test_done_marker_top_level_error_is_redacted_before_poll_output() -> None:
    result = parse_result(
        {
            "trigger_id": "run.folder.0",
            "status": "failed",
            "steps": [
                {
                    "step_name": "step-0",
                    "status": "failed",
                    "exit_code": 1,
                    "duration_seconds": 1.0,
                    "output": "full output remains at the done-marker S3 pointer",
                }
            ],
            "error": "token=engine-secret " + ("x" * 1000),
        },
        "run.folder.0",
    )

    assert result.error is not None
    assert "engine-secret" not in result.error
    assert "***" in result.error
    assert len(result.error) <= MAX_TERMINAL_EVIDENCE_TEXT_CHARS


def test_failure_manifest_and_pr_rendering_share_redaction_policy() -> None:
    reason = write_failure_manifest._failure_reason(
        {"failure_reason": "password=manifest-secret " + ("x" * 1000)}
    )
    rendered = folder_comment(
        "infra/app",
        {
            "status": "infrastructure_error",
            "account_id": "123456789012",
            "error": "token=render-secret",
        },
        {},
    )

    assert "manifest-secret" not in reason
    assert "render-secret" not in rendered
    assert "***" in reason
    assert "***" in rendered


def test_pipeline_failure_renderer_redacts_and_bounds_its_input() -> None:
    result = _render_pipeline_failure(
        {
            "run_id": "run",
            "pipeline_failure": {
                "failed_step": "token=pipeline-secret " + ("x" * 1000)
            },
            "webhook_info": {"notification_target": {"type": "registry"}},
        }
    )

    failed_step = result["failed_step"]
    assert isinstance(failed_step, str)
    assert "pipeline-secret" not in failed_step
    assert "***" in failed_step
    assert len(failed_step) <= MAX_TERMINAL_EVIDENCE_TEXT_CHARS


def test_engine_acceptance_survives_progress_comment_failure(monkeypatch) -> None:
    order: list[str] = []
    accepted: list[dict] = []
    notification: list[dict] = []
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setenv("ENGINE_CODEBUILD_PROJECT_NAME", "openci-tf-worker")

    def persist_acceptance(**kwargs):
        order.append("accepted")
        accepted.append(kwargs)

    def persist_notification(**kwargs):
        order.append("notification")
        notification.append(kwargs)

    def fail_comment(**_kwargs):
        order.append("comment")
        raise RuntimeError("token=github-secret GitHub unavailable")

    from src.services.run_folder import publish_mutation_progress

    monkeypatch.setattr(prepare_and_submit, "put_folder_submission", persist_acceptance)
    monkeypatch.setattr(
        "src.services.run_folder.notify.record_folder_submission_notification",
        persist_notification,
    )
    monkeypatch.setattr(
        publish_mutation_progress, "publish_codebuild_link", fail_comment
    )

    event = {
        "run_id": "run",
        "folder": "infra/app",
        "repo_name": "org/repo",
        "action": "apply",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
    }
    result: dict[str, object] = {
        "exec_id": "run.infra-app.0",
        "attempt": 0,
        "submitted_at": 100.0,
        "submission_status": "accepted",
        "engine_execution_arn": "arn:aws:states:us-east-1:123456789012:execution:engine:run",
        "codebuild_build_id": "openci-tf-worker:11111111-2222-3333-4444-555555555555",
    }

    prepare_and_submit._persist_submission_acknowledgement(
        event=event,
        account_id="123456789012",
        result=result,
    )
    result.update(
        prepare_and_submit._notify_after_acceptance(
            event=event,
            config=FolderConfig(account_alias="target"),
            lane_mode="apply",
            result=result,
        )
    )

    assert order == ["accepted", "comment", "notification"]
    assert accepted[0]["execution_id"] == result["exec_id"]
    assert accepted[0]["engine_execution_arn"] == result["engine_execution_arn"]
    assert result["submission_status"] == "accepted"
    assert result["notification_status"] == "failed"
    assert result["notification_failed"] is True
    assert "github-secret" not in str(result["notification_error"])
    assert notification[0]["notification_status"] == "failed"
    assert notification[0]["notification_error"] == result["notification_error"]


def test_persist_submission_acknowledgement_rejects_non_integer_attempt(monkeypatch) -> None:
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    event = {"run_id": "run", "folder": "infra/app"}
    base_result: dict[str, object] = {
        "exec_id": "run.infra-app.0",
        "submitted_at": 100.0,
    }

    with pytest.raises(TypeError, match="integer attempt"):
        prepare_and_submit._persist_submission_acknowledgement(
            event=event,
            account_id="123456789012",
            result={**base_result, "attempt": True},
        )

    with pytest.raises(TypeError, match="integer attempt"):
        prepare_and_submit._persist_submission_acknowledgement(
            event=event,
            account_id="123456789012",
            result={**base_result, "attempt": "0"},
        )


def test_prepare_handler_keeps_engine_acceptance_when_comment_fails(
    monkeypatch, tmp_path
) -> None:
    order: list[str] = []
    persisted: list[dict] = []
    captured_payload: dict[str, object] = {}
    monkeypatch.setenv("LANE_MODE", "apply")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setenv("ENGINE_CODEBUILD_STATE_MACHINE_ARN", "arn:engine-machine")
    monkeypatch.setenv("ENGINE_CODEBUILD_PROJECT_NAME", "openci-tf-worker")
    monkeypatch.setenv("PROJECT_NAME", "openci-tf")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        prepare_and_submit.boto3,
        "Session",
        lambda: type("Session", (), {"get_credentials": lambda self: None})(),
    )
    monkeypatch.setattr(
        prepare_and_submit, "_validated_external_id", lambda *_args: "external"
    )
    monkeypatch.setattr(
        prepare_and_submit.sts,
        "assume_role",
        lambda *_args, **_kwargs: {"AWS_ACCESS_KEY_ID": "target"},
    )
    monkeypatch.setattr(
        prepare_and_submit.sts,
        "get_caller_account_id",
        lambda credentials=None: "123456789012" if credentials else "999999999999",
    )
    monkeypatch.setattr(prepare_and_submit.s3, "presign_get", lambda *_args: "get")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_put", lambda *_args, **_kwargs: "put")
    monkeypatch.setattr(prepare_and_submit.s3, "head_object", lambda *_args: None)
    monkeypatch.setattr(
        prepare_and_submit, "_pinned_plan_secrets", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        prepare_and_submit,
        "shallow_clone",
        lambda *_args, **_kwargs: str(tmp_path),
    )
    monkeypatch.setattr(
        prepare_and_submit, "cleanup_clone", lambda _path: order.append("cleanup")
    )
    monkeypatch.setattr(prepare_and_submit, "get_github_token", lambda _path: "token")

    def engine_submit(_arn, payload):
        order.append("engine")
        captured_payload.update(payload)
        return {
            "engine_execution_arn": "arn:engine-execution",
            "codebuild_build_id": "openci-tf-worker:11111111-2222-3333-4444-555555555555",
        }

    def prepare_engine(**kwargs):
        acknowledgement = kwargs["submit"](kwargs["payload"])
        return {"submitted_at": 100.0, **acknowledgement}

    def persist_acceptance(**kwargs):
        order.append("accepted")
        persisted.append({"kind": "accepted", **kwargs})

    def persist_notification(**kwargs):
        order.append("notification")
        persisted.append({"kind": "notification", **kwargs})

    def fail_comment(**_kwargs):
        order.append("comment")
        raise RuntimeError("password=github-secret")

    from src.services.run_folder import publish_mutation_progress

    monkeypatch.setattr(
        prepare_and_submit.engine, "start_codebuild_execution", engine_submit
    )
    monkeypatch.setattr(prepare_and_submit, "prepare_and_submit", prepare_engine)
    monkeypatch.setattr(prepare_and_submit, "put_folder_submission", persist_acceptance)
    monkeypatch.setattr(
        "src.services.run_folder.notify.record_folder_submission_notification",
        persist_notification,
    )
    monkeypatch.setattr(
        publish_mutation_progress, "publish_codebuild_link", fail_comment
    )

    result = prepare_and_submit.handler(
        {
            "action": "apply",
            "run_id": "run",
            "folder": "infra/app",
            "budget": 900,
            "deadline_at": "2099-01-01T00:00:00Z",
            "attempt": 0,
            "upstream_urls": {"tofu": "https://tofu"},
            "folder_config": {"account_alias": "target"},
            "account_id": "123456789012",
            "account_binding": ["readonly", "poweruser", "external", 3600],
            "folder_pin": {
                "account_id": "123456789012",
                "tf_runtime": "tofu:1.8.0",
                "source_run_id": "source",
                "plan_sha256": "a" * 64,
                "plan_artifact_name": "plan.tfplan",
            },
            "git_url": "https://github.com/org/repo.git",
            "commit_hash": "a" * 40,
            "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
            "repo_name": "org/repo",
        },
        object(),
    )

    assert order == ["engine", "accepted", "cleanup", "comment", "notification"]
    assert result["submission_status"] == "accepted"
    assert result["notification_status"] == "failed"
    assert result["notification_failed"] is True
    assert "github-secret" not in str(result["notification_error"])
    assert persisted[0]["engine_execution_arn"] == "arn:engine-execution"
    assert persisted[1]["notification_status"] == "failed"
    assert set(captured_payload) == {
        "trigger_id",
        "s3_package_uri",
        "sops_type",
        "sops_path",
        "commands_b64",
        "done_endpoint",
        "execution_target",
        "timeout_seconds",
    }


def test_submission_ack_registry_write_never_changes_acceptance_on_notification(
    monkeypatch,
) -> None:
    writes: list[dict] = []
    updates: list[dict] = []

    class Table:
        def put_item(self, **kwargs):
            writes.append(kwargs)

        def update_item(self, **kwargs):
            updates.append(kwargs)

    monkeypatch.setattr(run_registry._shared, "_table", lambda: Table())
    run_registry.put_folder_submission(
        run_id="run",
        folder="infra/app",
        account_id="123456789012",
        execution_id="run.infra-app.0",
        attempt=0,
        submitted_at=100.0,
        engine_execution_arn="arn:engine",
    )
    run_registry.record_folder_submission_notification(
        run_id="run",
        folder="infra/app",
        execution_id="run.infra-app.0",
        attempt=0,
        notification_status="failed",
        notification_error="token=secret",
    )

    assert writes[0]["Item"]["status"] == "accepted"
    assert writes[0]["Item"]["notification_status"] == "pending"
    assert writes[0]["Item"]["notification_failed"] is False
    assert "#status = :accepted" in updates[0]["ConditionExpression"]
    assert "#status" not in updates[0]["UpdateExpression"]
    assert updates[0]["ExpressionAttributeValues"][":notification_failed"] is True
    assert updates[0]["ExpressionAttributeValues"][":notification_error"] == (
        "token=***"
    )


def test_submission_ack_replay_keeps_original_authoritative_timestamp(
    monkeypatch,
) -> None:
    existing = {
        "pk": "run#run",
        "sk": "submission#opaque#attempt#0000",
        "run_id": "run",
        "folder": "infra/app",
        "account_id": "123456789012",
        "execution_id": "run.infra-app.0",
        "attempt": 0,
        "status": "accepted",
        "submitted_at": "100.0",
        "notification_status": "failed",
        "engine_execution_arn": "arn:engine",
        "updated_at": 100,
        "expire_ttl": 200,
    }

    class Table:
        def put_item(self, **_kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "exists",
                    }
                },
                "PutItem",
            )

        def get_item(self, **_kwargs):
            replay = dict(existing)
            replay["sk"] = run_registry.folder_submission_sk("infra/app", 0)
            return {"Item": replay}

    monkeypatch.setattr(run_registry._shared, "_table", lambda: Table())
    acknowledgement = run_registry.put_folder_submission(
        run_id="run",
        folder="infra/app",
        account_id="123456789012",
        execution_id="run.infra-app.0",
        attempt=0,
        submitted_at=200.0,
        engine_execution_arn="arn:engine",
    )

    assert acknowledgement["submitted_at"] == "100.0"
    assert acknowledgement["notification_status"] == "failed"


def test_every_terminal_path_calls_the_shared_redactor() -> None:
    modules = (
        poll_done,
        collect,
        write_failure_manifest,
        render_handler,
        manifest,
        engine_result,
        engine_summary,
        artifact_formatters,
        run_registry._shared,
        run_folder_notify,
    )
    for module in modules:
        source = inspect.getsource(module)
        assert "redact_and_bound_terminal_evidence(" in source, module.__name__
        assert "[:2048]" not in source, module.__name__

    iam = Path("infra/deploy/modules/run_folder/iam.tf").read_text()
    prepare_policy = iam.split(
        'resource "aws_iam_role_policy" "prepare"', 1
    )[1].split('resource "aws_iam_role_policy" "poll_done"', 1)[0]
    for permission in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"):
        assert permission in prepare_policy
