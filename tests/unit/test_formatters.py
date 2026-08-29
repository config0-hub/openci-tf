# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden tests for the artifact-driven Phase-2 PR formatter contract."""
from pathlib import Path

import pytest

from src.domain.formatters.artifacts import (
    closed_pr_rejection_comment,
    command_context_block,
    folder_comment,
    summary,
)

FIXTURES = Path("tests/fixtures/artifacts")
_FIXTURE_FILES = {
    "init.out": "init.txt",
    "validate.out": "validate.txt",
    "tf/plan.out": "plan.txt",
    "tfsec.json": "tfsec.json",
    "infracost.json": "infracost.json",
}


def _fixture_text(name: str) -> str:
    return (FIXTURES / _FIXTURE_FILES[name]).read_text()



def test_summary_plan_destroy_uses_destroy_output_and_security():
    account = "123456789012"
    rendered = summary(
        [{"folder": "infra/vpc", "succeeded": True, "account_id": account}],
        {
            "infra/vpc": {
                "destroy.plan.out": "Plan: 0 to add, 0 to change, 2 to destroy",
                "tfsec.json": '{"results":[]}',
                "infracost.json": '{"totalMonthlyCost":"0"}',
            }
        },
        action="plan_destroy",
    )
    assert "## openci-tf plan --destroy" in rendered
    assert "| Folder | Drift | Security | Cost |" in rendered
    assert "| `infra/vpc` | ⚠️ | ✅ | $0 |" in rendered


def test_summary_plan_run_with_adds_only_shows_icon_cells():
    account = "123456789012"
    rendered = summary(
        [{"folder": "infra/vpc", "succeeded": True, "account_id": account}],
        {
            "infra/vpc": {
                "tf/plan.out": "Plan: 3 to add, 0 to change, 0 to destroy",
                "tfsec.json": '{"results":[]}',
                "infracost.json": '{"totalMonthlyCost":"0"}',
            }
        },
        action="plan",
    )
    assert "| Folder | Drift | Security | Cost |" in rendered
    assert "| `infra/vpc` | ⚠️ | ✅ | $0 |" in rendered


def test_summary_plan_run_with_zero_delta_shows_clean_icons():
    account = "123456789012"
    rendered = summary(
        [{"folder": "infra/vpc", "succeeded": True, "account_id": account}],
        {
            "infra/vpc": {
                "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
                "tfsec.json": '{"results":[]}',
                "infracost.json": '{"totalMonthlyCost":"0"}',
            }
        },
        action="plan",
    )
    assert "| `infra/vpc` | ✅ | ✅ | $0 |" in rendered


def test_summary_drift_run_uses_report_columns():
    account = "123456789012"
    rendered = summary(
        [{"folder": "infra/vpc", "succeeded": True, "account_id": account}],
        {
            "infra/vpc": {
                "tf/plan.out": "Plan: 3 to add, 0 to change, 0 to destroy",
                "tfsec.json": '{"results":[]}',
                "infracost.json": '{"totalMonthlyCost":"0"}',
            }
        },
        action="drift",
    )
    assert "## openci-tf drift" in rendered
    assert "| Folder | Drift | Security | Cost |" in rendered
    assert "| `infra/vpc` | ⚠️ | ✅ | $0 |" in rendered


def test_summary_drift_run_clean_when_no_delta():
    account = "123456789012"
    rendered = summary(
        [{"folder": "infra/vpc", "succeeded": True, "account_id": account}],
        {
            "infra/vpc": {
                "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
                "tfsec.json": '{"results":[]}',
                "infracost.json": '{"totalMonthlyCost":"0"}',
            }
        },
        action="drift",
    )
    assert "| `infra/vpc` | ✅ | ✅ |" in rendered


def test_summary_table_uses_real_artifact_producer_shapes():
    account = "123456789012"
    rendered = summary([
        {"folder": "good", "account_id": account, "succeeded": True},
        {"folder": "drift", "account_id": account, "succeeded": True},
        {"folder": "failed", "account_id": account, "status": "failed"},
        {"folder": "broken", "account_id": account, "status": "infrastructure_error"},
    ], {
        "good": {
            "tf/plan.out": "not a plan",
            "tfsec.json": '{"results":[]}',
            "infracost.json": '{"totalMonthlyCost":"0"}',
        },
        "drift": {
            "tf/plan.out": "Plan: 0 to add, 1 to change, 0 to destroy",
            "tfsec.json": '{"results":[{"severity":"MEDIUM"}]}',
            "infracost.json": '{"totalMonthlyCost":"12.50"}',
        },
    }, action="plan")
    assert "| Folder | Drift | Security | Cost |" in rendered
    assert "| `drift` | ⚠️ | ⚠️ | $12.50 |" in rendered
    assert "| `broken` | ❌ | ⏭️ |" in rendered


def test_folder_comment_uses_report_layout_for_plan():
    artifacts = {name: _fixture_text(name) for name in ("init.out", "validate.out", "tf/plan.out", "tfsec.json", "infracost.json")}
    rendered = folder_comment("infra/good", {"status": "succeeded", "account_id": "123456789012"}, artifacts, action="plan")
    assert "infra/good · Drift" in rendered
    for heading in ("> <summary>Setup", "> <summary>Plan", "> <summary>Security", "> <summary>Cost"):
        assert heading in rendered


def test_folder_comment_redacts_confirm_token_in_artifact_output():
    rendered = folder_comment(
        "infra/good",
        {"status": "succeeded", "account_id": "123456789012"},
        {
            "init.out": "init ok",
            "validate.out": "Success! The configuration is valid.",
            "tf/plan.out": 'output message = "confirm abc123"',
            "tfsec.json": '{"results":[]}',
            "infracost.json": '{"totalMonthlyCost":"0"}',
        },
        action="plan",
    )
    assert "confirm abc123" not in rendered
    assert "confirm <redacted>" in rendered


def test_folder_comment_redacts_confirm_token_in_terminal_error():
    rendered = folder_comment(
        "infra/good",
        {
            "status": "failed",
            "succeeded": False,
            "account_id": "123456789012",
            "error": "terraform failed after confirm abc123",
        },
        {},
        action="plan",
    )
    assert "confirm abc123" not in rendered
    assert "confirm <redacted>" in rendered


def test_folder_comment_plan_destroy_uses_destroy_output():
    rendered = folder_comment(
        "infra/good",
        {"status": "succeeded", "account_id": "123456789012"},
        {
            "init.out": "init ok",
            "validate.out": "valid",
            "destroy.plan.out": "Plan: 0 to add, 0 to change, 1 to destroy",
            "tfsec.json": '{"results":[]}',
            "infracost.json": '{"totalMonthlyCost":"0"}',
        },
        action="plan_destroy",
        run_id="1700000000000.deadbeef",
        repo_name="org/repo",
        pr_number=5,
    )
    assert "infra/good · Drift" in rendered
    assert "1 to destroy" in rendered
    assert "> <summary>Plan" in rendered


@pytest.mark.parametrize("action", ["plan"])
def test_folder_comment_plan_includes_security_and_cost_children(action):
    artifacts = {name: _fixture_text(name) for name in ("init.out", "validate.out", "tf/plan.out", "tfsec.json", "infracost.json")}
    rendered = folder_comment("infra/good", {"status": "succeeded", "account_id": "123456789012"}, artifacts, action=action)

    for heading in ("> <summary>Setup", "> <summary>Plan", "> <summary>Security", "> <summary>Cost"):
        assert heading in rendered


def test_folder_comment_report_uses_report_layout():
    artifacts = {
        name: _fixture_text(name)
        for name in (
            "init.out",
            "validate.out",
            "tf/plan.out",
            "tfsec.json",
            "infracost.json",
        )
    }
    artifacts["tfsec.output"] = _fixture_text("tfsec.json")
    rendered = folder_comment(
        "infra/good",
        {"status": "succeeded", "account_id": "123456789012"},
        artifacts,
        action="report",
        existing_names=frozenset(artifacts),
        tmp_bucket="tmp-bucket",
        region="us-east-1",
    )
    assert "infra/good · Drift" in rendered
    assert "### Plan\n<details>" not in rendered
    assert "> <summary>Plan" in rendered
    assert "```diff" in rendered
    assert "> <summary>Setup" in rendered
    assert "> <summary>Security" in rendered
    assert "> <summary>Cost" in rendered


@pytest.mark.parametrize("artifacts", [
    {name: _fixture_text(name) for name in ("init.out", "validate.out", "tf/plan.out", "tfsec.json", "infracost.json")},
])
def test_folder_comment_drift_uses_report_layout(artifacts):
    rendered = folder_comment("infra/good", {"status": "succeeded", "account_id": "123456789012"}, artifacts, action="drift")

    for heading in ("> <summary>Setup", "> <summary>Plan", "> <summary>Security", "> <summary>Cost"):
        assert heading in rendered


def test_folder_comment_null_error_renders_unknown_error():
    rendered = folder_comment("terraform/eu-west-1", {"succeeded": False, "error": None, "account_id": "123456789012"}, {})
    assert "unknown error" in rendered
    assert "None" not in rendered


def test_folder_comment_uses_derived_error_when_present():
    rendered = folder_comment(
        "terraform/eu-west-1",
        {"succeeded": False, "error": "api error AccessDenied: iam:GetRole", "account_id": "123456789012"},
        {},
    )
    assert "iam:GetRole" in rendered
    assert "unknown error" not in rendered


def test_command_context_block_redacts_confirm_token():
    block = command_context_block(
        action="destroy",
        comment_body="tf destroy confirm super-secret-token",
        comment_id=42,
        comment_link="https://github.com/org/repo/pull/1#issuecomment-42",
        run_id="run-1",
        commit_hash="a" * 40,
    )
    assert "### openci-tf command" in block
    assert "super-secret-token" not in block
    assert "confirm <redacted>" in block
    assert "- triggering comment: [42]" in block


def test_closed_pr_rejection_comment_does_not_echo_confirm_token():
    body = closed_pr_rejection_comment(
        comment_id=99,
        comment_link="https://github.com/org/repo/pull/1#issuecomment-99",
        comment_body="tf destroy confirm abcdef123456",
    )
    assert body.startswith("openci-tf ignored")
    assert "abcdef123456" not in body
    assert "confirm <redacted>" in body
