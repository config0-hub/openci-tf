# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipeline plan focus mode: skip security/cost and render plan-only previews."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.formatters.artifacts import pipeline_plan_preview_comment
from src.services.render import handler as render_handler


def test_pipeline_plan_script_omits_tfsec_and_infracost():
    script = render(
        ScriptParams(
            "plan",
            "lambda",
            pipeline_plan_focus=True,
        )
    )
    assert "tfsec" not in script
    assert "infracost" not in script


def test_regular_plan_script_still_runs_tfsec_and_infracost():
    script = render(ScriptParams("plan", "lambda"))
    assert "tfsec" in script
    assert "infracost" in script


def test_pipeline_plan_preview_orders_steps_and_omits_report_sections():
    folder_a = "terraform/primary/ap-northeast-1/05-s3-bucket"
    folder_b = "terraform/primary/ap-northeast-1/04-cloudwatch-log-group"
    artifacts = {
        folder_a: {
            "tf/plan.out": "Plan: 3 to add, 0 to change, 0 to destroy\n+ resource aws_s3_bucket.this",
        },
        folder_b: {
            "tf/plan.out": "Plan: 1 to add, 0 to change, 0 to destroy\n+ resource aws_cloudwatch_log_group.this",
        },
    }
    outcomes = [
        {"folder": folder_a, "succeeded": True, "account_id": "123456789012"},
        {"folder": folder_b, "succeeded": True, "account_id": "123456789012"},
    ]
    steps = [[folder_a], [folder_b]]
    body = pipeline_plan_preview_comment(
        outcomes,
        artifacts,
        action="plan",
        steps=steps,
    )
    assert "Pipeline plan preview · apply order" in body
    assert "> <summary>Security" not in body
    assert "> <summary>Cost" not in body
    assert "Execution" not in body
    assert "Metadata" not in body
    assert body.index(folder_a) < body.index(folder_b)
    assert "| 1/2 |" in body
    assert "| 2/2 |" in body
    assert body.count("<details>") == body.count("</details>")


def test_destroy_pipeline_plan_preview_reverses_step_order():
    folder_a = "terraform/primary/ap-northeast-1/05-s3-bucket"
    folder_b = "terraform/primary/ap-northeast-1/04-cloudwatch-log-group"
    artifacts = {
        folder_a: {
            "destroy.plan.out": "Plan: 0 to add, 0 to change, 3 to destroy\n- resource aws_s3_bucket.this",
        },
        folder_b: {
            "destroy.plan.out": "Plan: 0 to add, 0 to change, 1 to destroy\n- resource aws_cloudwatch_log_group.this",
        },
    }
    outcomes = [
        {"folder": folder_a, "succeeded": True, "account_id": "123456789012"},
        {"folder": folder_b, "succeeded": True, "account_id": "123456789012"},
    ]
    steps = [[folder_b], [folder_a]]
    body = pipeline_plan_preview_comment(
        outcomes,
        artifacts,
        action="plan_destroy",
        steps=steps,
    )
    assert "Pipeline plan preview · destroy order" in body
    assert body.index(folder_b) < body.index(folder_a)
    assert "**3 to destroy**" in body or "3 to destroy" in body


def test_render_pipeline_plan_focus_posts_single_preview_comment(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render_handler.boto3,
        "resource",
        lambda *_: SimpleNamespace(Table=lambda _: object()),
    )
    monkeypatch.setattr(
        render_handler,
        "list_text_prefix",
        lambda *_args, **_kw: {
            "tf/plan.out": "Plan: 1 to add, 0 to change, 0 to destroy\n+ resource aws_s3_bucket.this",
        },
    )
    monkeypatch.setattr(render_handler, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render_handler.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render_handler, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render_handler, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(
        render_handler,
        "delete_acknowledged_command_comment",
        lambda *_, **__: [],
    )
    posted: list[tuple[str, str]] = []

    def capture(_client, _repo, _pr, body, action, folder, **kwargs):
        posted.append((folder, body))
        return 1

    monkeypatch.setattr(render_handler, "_delete_and_repost", capture)

    render_handler.handler(
        {
            "action": "plan",
            "pipeline_plan_focus": True,
            "run_id": "1788002454579.7e34ddd6",
            "steps": [["terraform/primary/ap-northeast-1/05-s3-bucket"]],
            "webhook_info": {
                "repo_name": "org/repo",
                "pr_number": 7,
                "commit_hash": "a" * 40,
                "comment_id": 42,
                "comment_body": "tf plan pipeline data/primary",
                "pipeline": "data/primary",
            },
            "settings": {"ssm_openci_tf_github_token": "/token"},
            "outcomes": [
                {
                    "folder": "terraform/primary/ap-northeast-1/05-s3-bucket",
                    "account_id": "123456789012",
                    "execution_id": "inner.plan.0",
                    "succeeded": True,
                }
            ],
            "skipped": [],
        },
        None,
    )

    folder_posts = [folder for folder, _ in posted if folder != "all"]
    assert folder_posts == []
    summary_body = next(body for folder, body in posted if folder == "all")
    assert "Pipeline plan preview" in summary_body
    assert "tfsec" not in summary_body
    assert "<summary>Metadata</summary>" not in summary_body
    assert "**Command:** `tf plan pipeline data/primary`" in summary_body
