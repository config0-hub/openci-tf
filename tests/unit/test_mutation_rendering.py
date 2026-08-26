# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Terminal apply/destroy PR comment rendering requirements."""

from __future__ import annotations

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
    assert "## Apply succeeded" in body or "## Destroy succeeded" in body
    assert "### Pinned plan" not in body
    assert "<details>" in body
    assert "<summary>Pinned plan (tofu show)</summary>" in body
    assert body.index("<details>") < body.index("```")


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


def test_render_mutation_terminal_comment_is_markerless(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(
        render,
        "list_text_prefix",
        lambda *_args, **_kw: {
            "plan-show.out": "Plan: 0 to add, 0 to change, 1 to destroy",
            "apply.out": "Apply complete! Resources: 1 added.",
            "destroy.out": "Destroy complete! Resources: 1 destroyed.",
        },
    )
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    comments: list[str] = []

    def capture(_client, _repo, _pr, body, action, folder, **kwargs):
        if action == "apply":
            comments.append(body)
        return 1

    monkeypatch.setattr(render, "_delete_and_repost", capture)
    monkeypatch.setattr(render, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_transient_status_comment", lambda *_, **__: [])

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
