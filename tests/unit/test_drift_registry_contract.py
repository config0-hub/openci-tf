"""Authoritative drift-result propagation and precedence tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.platform.aws import run_registry
from src.services.render import handler as render_handler


def test_run_registry_update_writes_explicit_false_drift_result(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    run_registry.update_run_status("run-1", "succeeded", drift_detected=False)

    update = table.update_item.call_args.kwargs
    assert "drift_detected = :drift_detected" in update["UpdateExpression"]
    assert update["ExpressionAttributeValues"][":drift_detected"] is False


def test_render_persists_folder_and_aggregate_drift_result(monkeypatch):
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    outcomes = [
        {
            "folder": "infra/a",
            "account_id": "123456789012",
            "exec_id": "run.a.0",
            "attempt": 0,
            "status": "succeeded",
            "succeeded": True,
            "drift_detected": False,
        },
        {
            "folder": "infra/b",
            "account_id": "123456789012",
            "exec_id": "run.b.0",
            "attempt": 0,
            "status": "succeeded",
            "succeeded": True,
            "drift_detected": True,
        },
    ]
    with patch("src.platform.aws.run_registry.put_folder_record") as put_folder, patch(
        "src.platform.aws.run_registry.update_run_status"
    ) as update_run:
        render_handler._update_run_registry(
            {"run_id": "run-1", "skipped": []},
            outcomes,
            "drift",
        )

    assert [call.kwargs["drift_detected"] for call in put_folder.call_args_list] == [False, True]
    update_run.assert_called_once_with("run-1", "succeeded", drift_detected=True)


def test_render_indexes_successful_pipeline_apply_step(monkeypatch):
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    outcomes = [
        {
            "folder": "infra/vpc",
            "account_id": "123456789012",
            "exec_id": "run.vpc.0",
            "attempt": 0,
            "status": "succeeded",
            "succeeded": True,
        }
    ]
    event = {
        "run_id": "run-1",
        "deadline_at": "2026-08-22T16:00:00Z",
        "skipped": [],
        "webhook_info": {
            "trigger_id": "trigger-1",
            "repo_name": "org/repo",
            "pipeline": "data/primary",
            "pipeline_step_index": 1,
            "pipeline_step_count": 2,
            "pipeline_sha256": "c" * 64,
        },
    }
    with patch("src.platform.aws.run_registry.put_folder_record"), patch(
        "src.platform.aws.run_registry.update_run_status"
    ) as update_run, patch(
        "src.platform.aws.run_registry.mark_pipeline_apply_succeeded"
    ) as mark_step:
        render_handler._update_run_registry(event, outcomes, "apply")

    update_run.assert_called_once_with("run-1", "succeeded", drift_detected=None)
    mark_step.assert_called_once_with(
        "run-1",
        trigger_id="trigger-1",
        repo_name="org/repo",
        pipeline="data/primary",
        step_index=1,
        step_count=2,
        pipeline_sha256="c" * 64,
    )


def test_failure_status_dominates_drift_projection():
    outcomes = [
        {"status": "succeeded", "succeeded": True, "drift_detected": True},
        {"status": "failed", "succeeded": False},
    ]

    assert render_handler._run_drift_detected(outcomes, "drift") is True
    assert render_handler._terminal_status(outcomes, []) == "failed"


def test_absent_drift_fields_remain_unknown_instead_of_false():
    assert render_handler._run_drift_detected([{"status": "succeeded"}], "drift") is None
    assert render_handler._run_drift_detected([], "drift") is None
    assert render_handler._run_drift_detected([{"drift_detected": False}], "plan") is None
