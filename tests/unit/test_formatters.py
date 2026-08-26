# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden tests for the artifact-driven Phase-2 PR formatter contract."""
from pathlib import Path

import pytest

from src.domain.formatters.artifacts import (
    closed_pr_rejection_comment,
    command_context_block,
    mutation_command_context_block,
    folder_comment,
    infracost,
    initialize,
    plan,
    summary,
    tfsec,
    validate,
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


@pytest.mark.parametrize(("formatter", "artifact", "golden"), [
    (initialize, "init.out", "init.md"),
    (validate, "validate.out", "validate.md"),
    (plan, "tf/plan.out", "plan.md"),
    (tfsec, "tfsec.json", "tfsec.md"),
    (infracost, "infracost.json", "infracost.md"),
])
def test_recorded_artifact_section_matches_golden(formatter, artifact, golden):
    assert formatter(_fixture_text(artifact)) == (FIXTURES / golden).read_text().rstrip("\n")


def test_summary_plan_run_with_adds_only_shows_plan_counts():
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
    assert "| Folder | Account | Plan | Security | Cost |" in rendered
    assert f"| `infra/vpc` | `{account}` | +3 ~0 -0 | clean | $0 |" in rendered


def test_summary_plan_run_with_zero_delta_shows_no_changes():
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
    assert f"| `infra/vpc` | `{account}` | no changes | clean | $0 |" in rendered


def test_summary_drift_run_keeps_drift_check_column():
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
    assert "| Folder | Account | Drift Check | Security | Cost |" in rendered
    assert f"| `infra/vpc` | `{account}` | changes | clean | $0 |" in rendered


def test_summary_drift_run_clean_when_no_delta():
    account = "123456789012"
    rendered = summary(
        [{"folder": "infra/vpc", "succeeded": True, "account_id": account}],
        {"infra/vpc": {"tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy"}},
        action="drift",
    )
    assert f"| `infra/vpc` | `{account}` | clean |" in rendered


def test_summary_table_uses_real_artifact_producer_shapes():
    account = "123456789012"
    rendered = summary([
        {"folder": "good", "account_id": account, "succeeded": True},
        {"folder": "drift", "account_id": account, "succeeded": True},
        {"folder": "failed", "account_id": account, "status": "failed"},
        {"folder": "broken", "account_id": account, "status": "infrastructure_error"},
    ], {"drift": {"tf/plan.out": "Plan: 0 to add, 1 to change, 0 to destroy", "tfsec.json": '{"results":[{"severity":"MEDIUM"}]}', "infracost.json": '{"totalMonthlyCost":"12.50"}'}}, action="plan")
    assert "| Folder | Account | Plan | Security | Cost |" in rendered
    assert f"| `drift` | `{account}` | +0 ~1 -0 | medium | $12.50 |" in rendered
    assert f"| `broken` | `{account}` | failed | not run | n/a |" in rendered


def test_folder_comment_uses_terraform_heading_for_plan():
    artifacts = {name: _fixture_text(name) for name in ("init.out", "validate.out", "tf/plan.out", "tfsec.json", "infracost.json")}
    rendered = folder_comment("infra/good", {"status": "succeeded", "account_id": "123456789012"}, artifacts, action="plan")
    assert "## Terraform: `infra/good` (123456789012)" in rendered
    for heading in ("Initialize", "Validate", "Plan", "Security Scan", "Cost Analysis"):
        assert heading in rendered


def test_folder_comment_plan_destroy_uses_destroy_output_and_pointer():
    rendered = folder_comment(
        "infra/good",
        {"status": "succeeded", "account_id": "123456789012"},
        {
            "init.out": "init ok",
            "validate.out": "valid",
            "destroy.plan.out": "Plan: 0 to add, 0 to change, 1 to destroy",
        },
        action="plan_destroy",
        run_id="1700000000000.deadbeef",
        repo_name="org/repo",
        pr_number=5,
    )
    assert "Plan_Destroy succeeded" in rendered
    assert "1 to destroy" in rendered
    assert "Destroy plan pointer" in rendered
    assert "openci-tf/org/repo/pr-5/infra/good/destroy.env" in rendered
    assert "plan.env" not in rendered


@pytest.mark.parametrize("action", ["plan", "report"])
def test_folder_comment_plan_and_report_include_scan_and_cost_sections(action):
    artifacts = {name: _fixture_text(name) for name in ("init.out", "validate.out", "tf/plan.out", "tfsec.json", "infracost.json")}
    rendered = folder_comment("infra/good", {"status": "succeeded", "account_id": "123456789012"}, artifacts, action=action)

    for heading in ("Initialize", "Validate", "Plan", "Security Scan", "Cost Analysis"):
        assert heading in rendered


@pytest.mark.parametrize("artifacts", [
    {name: _fixture_text(name) for name in ("init.out", "validate.out", "tf/plan.out")},
    {name: _fixture_text(name) for name in ("init.out", "validate.out", "tf/plan.out", "tfsec.json", "infracost.json")},
])
def test_folder_comment_drift_omits_scan_and_cost_sections(artifacts):
    rendered = folder_comment("infra/good", {"status": "succeeded", "account_id": "123456789012"}, artifacts, action="drift")

    for heading in ("Initialize", "Validate", "Plan"):
        assert heading in rendered
    assert "Security Scan" not in rendered
    assert "Cost Analysis" not in rendered


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


def test_mutation_command_context_block_redacts_and_lists_both_commands():
    block = mutation_command_context_block(
        action="apply",
        requested_comment_body="tf apply terraform/eu-west-1/02-ec2",
        requested_comment_id=10,
        confirmation_comment_body="tf apply confirm secret-token",
        confirmation_comment_id=11,
        run_id="run-apply",
        commit_hash="a" * 40,
        comments_removed=True,
    )
    assert "- requested command: `tf apply terraform/eu-west-1/02-ec2`" in block
    assert "- confirmation command: `tf apply confirm <redacted>`" in block
    assert "secret-token" not in block
    assert "requested comment id: `10` (removed after acknowledgement)" in block
    assert "confirmation comment id: `11` (removed after acknowledgement)" in block
