# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Terminal apply/destroy PR comment rendering requirements."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.domain.formatters.artifacts import mutation_terminal_comment, summary
from src.services.render import handler as render


def _mutation_terminal_body(
    *,
    action: str = "apply",
    plan_show_text: str | None = "Plan: 1 to add, 0 to change, 0 to destroy",
    plan_show_pointer: str | None = None,
) -> str:
    return mutation_terminal_comment(
        action=action,
        folder="terraform/eu-west-1/02-ec2",
        account_id="REPLACE_MAIN_ACCOUNT",
        commit_hash="a" * 40,
        succeeded=True,
        pinned_plan_artifact="plan.tfplan" if action == "apply" else "destroy.plan.tfplan",
        console_url="https://example.test/states",
        codebuild_url="https://example.test/codebuild",
        codebuild_account_id="REPLACE_MAIN_ACCOUNT",
        plan_show_text=plan_show_text,
        plan_show_pointer=plan_show_pointer,
    )


def _assert_plan_in_collapsed_details(body: str) -> None:
    assert "<details>" in body
    assert "> <summary>Plan " in body
    assert "<summary>Pinned plan (tofu show)</summary>" not in body
    assert body.index("<details>") < body.index("```")


_PLAN_TRUNCATION_NOTE = "Output truncated. See S3 artifacts for full plan output."


def _assert_plan_truncation_note_outside_fence(body: str) -> None:
    assert body.count(_PLAN_TRUNCATION_NOTE) == 1
    before_note, _, _after_note = body.partition(_PLAN_TRUNCATION_NOTE)
    plan_start = before_note.index("> <summary>Plan ")
    plan_section = before_note[plan_start:]
    fence_start = plan_section.index("```diff")
    fence_end = plan_section.index("```", fence_start + len("```diff"))
    fenced_body = plan_section[fence_start : fence_end + 3]
    assert _PLAN_TRUNCATION_NOTE not in fenced_body
    after_fence = plan_section[fence_end + 3 :]
    assert re.fullmatch(r"[\s>]*", after_fence), after_fence


@pytest.mark.parametrize("action", ["apply", "destroy"])
def test_terminal_mutation_success_includes_plan_in_collapsed_details(action):
    body = _mutation_terminal_body(action=action)
    _assert_plan_in_collapsed_details(body)


def test_terminal_mutation_does_not_inline_apply_or_destroy_logs():
    body = _mutation_terminal_body(
        plan_show_text="Terraform will perform the following actions:\n  # aws_instance.tracer",
    )
    assert "apply.out" not in body
    assert "destroy.out" not in body
    assert "```" in body


def test_mutation_plan_over_budget_truncates_with_note_outside_fence():
    plan_body = (
        "Plan: 1 to add, 0 to change, 0 to destroy\n"
        + ("+ resource aws_instance.probe\n" * 5_000)
    )
    assert len(plan_body) > 8_000
    body = _mutation_terminal_body(plan_show_text=plan_body)
    _assert_plan_truncation_note_outside_fence(body)


def test_summary_does_not_render_icon_legend():
    rendered = summary(
        [{"folder": "infra/a", "succeeded": True, "account_id": "123456789012"}],
        {
            "infra/a": {
                "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
                "tfsec.json": '{"results":[]}',
                "infracost.json": '{"totalMonthlyCost":"0"}',
            }
        },
    )
    assert "Legend" not in rendered
    assert "Drift:" not in rendered
    assert "Security:" not in rendered


def _stub_render_mutation_dependencies(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_args, **_kw: {
            "plan-show.out": "Plan: 0 to add, 0 to change, 1 to destroy",
            "apply.out": "Apply complete! Resources: 1 added.",
            "destroy.out": "Destroy complete! Resources: 1 destroyed.",
        },
    )
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_, **__: [])
    monkeypatch.setattr(render, "_cleanup_terminal_mutation_comments", lambda *_, **__: [])


def test_render_mutation_terminal_comment_is_markerless(monkeypatch):
    _stub_render_mutation_dependencies(monkeypatch)
    comments: list[str] = []

    def capture(_client, _repo, _pr, body, action, folder, **kwargs):
        if action == "apply":
            comments.append(body)
        return 1

    monkeypatch.setattr(render, "_delete_and_repost", capture)

    render.handler(
        {
            "action": "apply",
            "run_id": "1787000000000.abc12345",
            "webhook_info": {
                "repo_name": "<REPO_ORG>/<REPO_NAME>",
                "pr_number": 22,
                "commit_hash": "a" * 40,
            },
            "settings": {"ssm_openci_tf_github_token": "/token"},
            "outcomes": [
                {
                    "folder": "terraform/eu-west-1/02-ec2",
                    "account_id": "REPLACE_MAIN_ACCOUNT",
                    "execution_id": "inner.apply.0",
                    "succeeded": True,
                }
            ],
            "skipped": [],
        },
        None,
    )

    assert len(comments) == 1
    body = comments[0]
    assert "#openci-tf:::" not in body
    _assert_plan_in_collapsed_details(body)
    assert "Apply complete!" not in body
    assert "Destroy complete!" not in body


@pytest.mark.parametrize(
    ("action", "source_label", "source_run_id"),
    [
        ("apply", "source plan run id", "1787880280961.7e34ddd6"),
        ("destroy", "source destroy-plan run id", "1787884548233.c7ac9302"),
    ],
)
def test_render_mutation_terminal_comment_includes_source_plan_run_id(
    monkeypatch, action, source_label, source_run_id
):
    _stub_render_mutation_dependencies(monkeypatch)
    monkeypatch.setattr(
        render,
        "get_bounded_json",
        lambda *_args, **_kwargs: {"source_plan_run_id": source_run_id},
    )
    comments: list[str] = []

    def capture(_client, _repo, _pr, body, captured_action, folder, **kwargs):
        if captured_action == action and folder == "terraform/eu-west-1/02-ec2":
            comments.append(body)
        return 1

    monkeypatch.setattr(render, "_delete_and_repost", capture)

    render.handler(
        {
            "action": action,
            "run_id": "1787000000000.abc12345",
            "webhook_info": {
                "repo_name": "<REPO_ORG>/<REPO_NAME>",
                "pr_number": 22,
                "comment_id": 102,
                "comment_body": f"tf {action} confirm deadbee",
                "commit_hash": "a" * 40,
            },
            "requested_comment_id": 101,
            "requested_comment_body": f"tf {action} terraform/eu-west-1/02-ec2",
            "intent_comment_id": 103,
            "consumed_confirm_token": "deadbee",
            "settings": {"ssm_openci_tf_github_token": "/token"},
            "outcomes": [
                {
                    "folder": "terraform/eu-west-1/02-ec2",
                    "account_id": "REPLACE_MAIN_ACCOUNT",
                    "execution_id": "outer.child",
                    "output": {
                        "exec_id": "inner.mutation.0",
                        "succeeded": True,
                        "manifest_s3_uri": "s3://tmp/openci-tf/manifest.json",
                    },
                }
            ],
            "skipped": [],
        },
        None,
    )

    assert len(comments) == 1
    body = comments[0]
    assert f"- {source_label}: `{source_run_id}`" in body
    assert "deadbee" not in body
    assert f"tf {action} confirm <redacted>" in body
    assert "<summary>Metadata</summary>" in body


def _pipeline_apply_render_event(
    *,
    step_index: int,
    step_count: int,
    confirm_token: str = "deadbee",
) -> dict:
    return {
        "action": "apply",
        "run_id": "1787000000000.abc12345",
        "webhook_info": {
            "repo_name": "<REPO_ORG>/<REPO_NAME>",
            "pr_number": 22,
            "commit_hash": "a" * 40,
            "comment_id": 102,
            "comment_body": (
                f"tf apply pipeline data/primary step {step_index} confirm {confirm_token}"
            ),
            "pipeline": "data/primary",
            "pipeline_step_index": step_index,
            "pipeline_step_count": step_count,
        },
        "requested_comment_id": 101,
        "requested_comment_body": f"tf apply pipeline data/primary step {step_index}",
        "intent_comment_id": 103,
        "consumed_confirm_token": confirm_token,
        "settings": {"ssm_openci_tf_github_token": "/token"},
        "outcomes": [
            {
                "folder": "terraform/eu-west-1/02-ec2",
                "account_id": "REPLACE_MAIN_ACCOUNT",
                "execution_id": "inner.apply.0",
                "status": "succeeded",
                "succeeded": True,
            }
        ],
        "skipped": [],
    }


def _capture_pipeline_apply_terminal_body(monkeypatch, *, step_index: int, step_count: int) -> str:
    _stub_render_mutation_dependencies(monkeypatch)
    comments: list[str] = []

    def capture(_client, _repo, _pr, body, action, folder, **kwargs):
        if action == "apply" and folder == "terraform/eu-west-1/02-ec2":
            comments.append(body)
        return 1

    monkeypatch.setattr(render, "_delete_and_repost", capture)
    render.handler(
        _pipeline_apply_render_event(step_index=step_index, step_count=step_count),
        None,
    )
    assert len(comments) == 1
    return comments[0]


def _assert_pipeline_apply_body_order(body: str, *, note: str) -> None:
    metadata_marker = "<summary>Metadata</summary>"
    main_details_start = body.index("<details>")
    note_pos = body.index(note)
    metadata_pos = body.index(metadata_marker)

    assert main_details_start < note_pos < metadata_pos
    assert body.rstrip().endswith("</details>")
    assert "deadbee" not in body
    assert "confirm <redacted>" in body
    assert body.count(note) == 1
    assert body.count(metadata_marker) == 1


def test_render_pipeline_apply_next_step_body_order(monkeypatch):
    body = _capture_pipeline_apply_terminal_body(
        monkeypatch, step_index=1, step_count=2
    )
    note = "> [!NOTE]\n> Next step: `tf apply pipeline data/primary step 2`"
    _assert_pipeline_apply_body_order(body, note=note)
    _assert_plan_in_collapsed_details(body)


def test_render_pipeline_apply_completion_body_order(monkeypatch):
    body = _capture_pipeline_apply_terminal_body(
        monkeypatch, step_index=2, step_count=2
    )
    note = "> [!NOTE]\n> Pipeline `data/primary` complete (2 steps)."
    _assert_pipeline_apply_body_order(body, note=note)
    _assert_plan_in_collapsed_details(body)
