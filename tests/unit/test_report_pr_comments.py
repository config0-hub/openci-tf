# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for tf report GitHub PR comment presentation."""

from __future__ import annotations

import json

import re
from typing import cast

from src.domain.formatters.artifacts import (
    _MAX_COMMENT_CHARS,
    bound_comment,
    folder_comment,
    summary,
)
from src.domain.github.comment_object_id import format_comment_object_marker
from src.platform.github.client import GitHubClient
from src.services.render import handler as render_handler

_ACCOUNT = "123456789012"
_OTHER = "210987654321"
_FULL_SHA = "a" * 40
_REGION = "us-east-1"
_BUCKET = "tmp-bucket"
_HUB_ACCOUNT = "999999999999"
_IC_START = "https://d-9567aa6b98.awsapps.com/start"
_IC_ROLE = "AWSAdministratorAccess"

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


def _outcome(folder: str, **overrides):
    base = {
        "folder": folder,
        "account_id": _ACCOUNT,
        "status": "succeeded",
        "succeeded": True,
    }
    base.update(overrides)
    return base


def _artifacts(
    *,
    plan: str = "Plan: 0 to add, 0 to change, 0 to destroy",
    tfsec: str = '{"results":[]}',
    tfsec_output: str = "No problems detected!",
    infracost: str = '{"totalMonthlyCost":"0"}',
    infracost_output: str = "",
) -> dict[str, str]:
    artifacts = {
        "init.out": "Terraform has been successfully initialized!",
        "validate.out": "Success! The configuration is valid.",
        "tf/plan.out": plan,
        "tfsec.json": tfsec,
        "tfsec.output": tfsec_output,
        "infracost.json": infracost,
        "manifest.json": "{}",
    }
    if infracost_output:
        artifacts["infracost.output"] = infracost_output
    return artifacts


def _report_link_kwargs(**overrides):
    base = {
        "action": "report",
        "existing_names": frozenset(_artifacts().keys()),
        "tmp_bucket": _BUCKET,
        "region": _REGION,
        "hub_account_id": _HUB_ACCOUNT,
        "identity_center_start_url": _IC_START,
        "identity_center_role_name": _IC_ROLE,
        "run_id": "run-1",
        "repo_name": "org/repo",
        "console_url": "https://console.aws.example/run",
    }
    base.update(overrides)
    return base


def test_report_summary_uses_drift_header_and_icon_cells():
    rendered = summary(
        [_outcome("infra/a")],
        {"infra/a": _artifacts()},
        action="report",
    )
    assert "## openci-tf report" in rendered
    assert "**Type:** Report" in rendered
    assert "| Drift |" in rendered
    assert "| Folder | Drift | Security | Cost |" in rendered
    assert "| Account |" not in rendered
    assert "✅" in rendered
    assert "CLEAN" not in rendered


def test_report_summary_priority_ordering():
    outcomes = [
        _outcome("z-clean"),
        _outcome("a-failed", status="failed", succeeded=False),
        _outcome("b-unknown", account_id=_OTHER),
        _outcome("c-destroy", account_id=_OTHER),
        _outcome("d-high"),
        _outcome("e-medium", account_id=_OTHER),
        _outcome("f-drift", account_id=_OTHER),
    ]
    artifacts = {
        "z-clean": _artifacts(),
        "a-failed": _artifacts(),
        "b-unknown": _artifacts(plan="not a plan"),
        "c-destroy": _artifacts(
            plan="Plan: 0 to add, 0 to change, 2 to destroy",
            tfsec='{"results":[]}',
        ),
        "d-high": _artifacts(
            plan="Plan: 0 to add, 0 to change, 0 to destroy",
            tfsec='{"results":[{"severity":"HIGH"}]}',
        ),
        "e-medium": _artifacts(
            plan="Plan: 0 to add, 0 to change, 0 to destroy",
            tfsec='{"results":[{"severity":"MEDIUM"}]}',
        ),
        "f-drift": _artifacts(plan="Plan: 1 to add, 0 to change, 0 to destroy"),
    }
    rendered = summary(outcomes, artifacts, action="report")
    attention = rendered.split("### Needs attention", 1)[1].split("<details>", 1)[0]
    positions = [
        attention.index(f"`{folder}`")
        for folder in (
            "a-failed",
            "b-unknown",
            "d-high",
            "c-destroy",
            "e-medium",
            "f-drift",
        )
    ]
    assert positions == sorted(positions)
    assert "`z-clean`" not in attention


def test_report_summary_tie_breaks_by_folder_path():
    outcomes = [
        _outcome("infra/b"),
        _outcome("infra/a", account_id=_OTHER),
    ]
    artifacts = {
        "infra/a": _artifacts(plan="Plan: 1 to add, 0 to change, 0 to destroy"),
        "infra/b": _artifacts(plan="Plan: 1 to add, 0 to change, 0 to destroy"),
    }
    rendered = summary(outcomes, artifacts, action="report")
    attention = rendered.split("### Needs attention", 1)[1]
    assert attention.index("`infra/a`") < attention.index("`infra/b`")


def test_report_summary_shows_every_non_clean_row_and_collapses_clean():
    outcomes = [
        _outcome("infra/drift"),
        _outcome("infra/clean", account_id=_OTHER),
        _outcome("infra/also-clean", account_id=_OTHER),
    ]
    artifacts = {
        "infra/drift": _artifacts(plan="Plan: 2 to add, 0 to change, 0 to destroy"),
        "infra/clean": _artifacts(),
        "infra/also-clean": _artifacts(),
    }
    rendered = summary(outcomes, artifacts, action="report")
    assert "### Needs attention" in rendered
    assert "`infra/drift`" in rendered
    assert "2 clean folders ✅" in rendered
    assert rendered.count("<details>") == 1
    clean_section = rendered.split("2 clean folders ✅", 1)[1]
    assert "`infra/clean`" in clean_section
    assert "`infra/also-clean`" in clean_section
    attention_section = rendered.split("### Needs attention", 1)[1].split(
        "2 clean folders ✅", 1
    )[0]
    assert "`infra/drift`" in attention_section
    assert "`infra/clean`" not in attention_section
    assert "`infra/also-clean`" not in attention_section


def test_report_summary_all_clean_verdict():
    rendered = summary(
        [_outcome("infra/a"), _outcome("infra/b", account_id=_OTHER)],
        {"infra/a": _artifacts(), "infra/b": _artifacts()},
        action="report",
    )
    assert "### Needs attention" not in rendered
    assert "2 clean folders ✅" in rendered


def test_report_folder_reveals_plan_with_one_expansion():
    plan_body = (
        "Plan: 1 to add, 0 to change, 1 to destroy\n"
        "+ resource aws_instance.example\n"
        "- resource aws_instance.old\n"
    )
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(plan=plan_body),
        **_report_link_kwargs(),
    )
    assert rendered.startswith("<details>")
    inner = rendered.split("</summary>", 1)[1]
    assert inner.count("<details>") >= 5
    assert "> <summary>Plan" in inner
    assert "```diff" in inner
    assert "+ resource aws_instance.example" in inner
    assert "- resource aws_instance.old" in inner
    assert "### Plan" not in rendered


def test_report_folder_inline_plan_preserves_add_change_destroy():
    plan_body = (
        "Plan: 2 to add, 3 to change, 1 to destroy\n"
        "+ resource aws_s3_bucket.new\n"
        "! resource aws_s3_bucket.changed\n"
        "- resource aws_s3_bucket.old\n"
    )
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(plan=plan_body),
        **_report_link_kwargs(),
    )
    assert "**2 to add**" in rendered
    assert "**3 to change**" in rendered
    assert "**1 to destroy**" in rendered
    assert "+ resource aws_s3_bucket.new" in rendered


def test_report_folder_collapsible_sections_have_informative_summaries():
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        {
            **_artifacts(
                plan="Plan: 1 to add, 0 to change, 0 to destroy",
                tfsec=json.dumps(
                    {
                        "results": [
                            {"severity": "HIGH", "rule_description": "bad"},
                            {"severity": "LOW", "rule_description": "meh"},
                        ]
                    }
                ),
                tfsec_output="Result #1 HIGH bad",
                infracost='{"totalMonthlyCost":"12.50"}',
                infracost_output="Monthly cost $12.50",
            ),
        },
        **_report_link_kwargs(),
    )
    assert "infra/a · Drift ⚠️ · Security 🛑" in rendered.split("</summary>", 1)[0]
    assert "> <summary>Security 🛑</summary>" in rendered
    assert "<summary>Cost · $12.50/mo</summary>" in rendered
    assert "> <summary>Setup ✅</summary>" in rendered
    assert "> <summary>Execution</summary>" in rendered
    assert "> <summary>Artifacts</summary>" in rendered
    assert "Result #1 HIGH bad" in rendered
    assert "Monthly cost $12.50" in rendered
    assert _ACCOUNT not in rendered.split("</summary>", 1)[0]


def test_report_marker_survives_bounding():
    marker = format_comment_object_marker("org/repo", 7, "report", "infra/a")
    body = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        {
            **_artifacts(
                plan="Plan: 1 to add, 0 to change, 0 to destroy.\n"
                + ("+ resource aws_instance.probe\n" * 20_000)
            ),
        },
        **_report_link_kwargs(),
    )
    rendered = bound_comment(body, max_chars=8_000, suffix=f"\n\n{marker}")
    assert len(rendered) <= 8_000
    assert rendered.endswith(marker)
    assert rendered.count(marker) == 1


def test_report_all_marker_survives_bounding():
    marker = format_comment_object_marker("org/repo", 7, "report-all", "all")
    body = summary(
        [_outcome(f"infra/{index}") for index in range(30)],
        {
            f"infra/{index}": _artifacts(
                plan=f"Plan: {index} to add, 0 to change, 0 to destroy"
            )
            for index in range(30)
        },
        action="report",
    )
    rendered = bound_comment(body, max_chars=_MAX_COMMENT_CHARS, suffix=f"\n\n{marker}")
    assert len(rendered) <= _MAX_COMMENT_CHARS
    assert rendered.endswith(marker)
    assert rendered.count(marker) == 1


def test_plan_summary_uses_unified_report_layout():
    rendered = summary(
        [_outcome("infra/a")],
        {"infra/a": _artifacts()},
        action="plan",
    )
    assert "## openci-tf plan" in rendered
    assert "**Type:** Plan" in rendered
    assert "| Folder | Drift | Security | Cost |" in rendered
    assert "| Account |" not in rendered
    assert "| `infra/a` | ✅ | ✅ | $0 |" in rendered
    assert "✅ CLEAN" not in rendered


def test_report_summary_keeps_valid_security_when_plan_is_unknown():
    rendered = summary(
        [_outcome("infra/a")],
        {
            "infra/a": _artifacts(
                plan="not a parseable plan",
                tfsec=json.dumps({"results": [{"severity": "HIGH"}]}),
            )
        },
        action="report",
    )
    assert "❔" in rendered
    assert "🛑" in rendered


def test_report_summary_skipped_and_pending_are_honest_not_high_risk_alerts():
    rendered = summary(
        [
            _outcome("infra/skipped", status="skipped"),
            _outcome("infra/pending", status="in_progress"),
        ],
        {},
        action="report",
    )
    assert "⏭️" in rendered
    assert "⏳" in rendered
    assert "high-risk" not in rendered
    assert "review drift or security findings" not in rendered


def test_report_summary_unknown_status_needs_attention_even_with_clean_artifacts():
    rendered = summary(
        [_outcome("infra/unknown", status="unknown", succeeded=True)],
        {"infra/unknown": _artifacts()},
        action="report",
    )
    assert "### Needs attention" in rendered
    assert "`infra/unknown`" in rendered
    assert "❔" in rendered
    assert "clean folders ✅" not in rendered


def test_report_security_summary_distinguishes_critical():
    rendered = summary(
        [_outcome("infra/a")],
        {
            "infra/a": _artifacts(
                tfsec=json.dumps(
                    {
                        "results": [
                            {"severity": "HIGH"},
                            {"severity": "CRITICAL"},
                        ]
                    }
                )
            )
        },
        action="report",
    )
    assert "🛑" in rendered


def test_report_folder_invalid_tfsec_evidence_renders_unknown_security():
    invalid_tfsec_payloads = [
        "[]",
        "null",
        '"scalar"',
        "12",
        '{"results":{}}',
        '{"results":["bad-entry"]}',
        '{"results":[{"severity":""}]}',
    ]
    for payload in invalid_tfsec_payloads:
        rendered = folder_comment(
            "infra/a",
            _outcome("infra/a"),
            _artifacts(tfsec=payload, tfsec_output=""),
            **_report_link_kwargs(),
        )
        assert "> <summary>Security ❔</summary>" in rendered
        assert "Security output unavailable." in rendered


def test_report_folder_security_stays_expandable_when_clean():
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(),
        **_report_link_kwargs(),
    )
    assert "> <summary>Security ✅</summary>" in rendered


def test_report_folder_artifacts_use_authenticated_console_links():
    names = frozenset(
        {
            "manifest.json",
            "init.out",
            "validate.out",
            "tf/plan.out",
            "tfsec.json",
            "tfsec.output",
            "infracost.json",
        }
    )
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(),
        **_report_link_kwargs(existing_names=names),
    )
    assert _IC_START in rendered
    assert f"account_id={_HUB_ACCOUNT}" in rendered
    assert f"role_name={_IC_ROLE}" in rendered
    assert "[manifest.json]" in rendered
    assert "[init.out]" in rendered
    assert "s3://" not in rendered


def test_report_folder_artifact_links_use_pr_scoped_report_prefix():
    from urllib.parse import unquote

    from src.domain.engine.artifact_paths import build_folder_artifact_keys_for_run

    repo_name = "org/repo"
    run_id = "1756419360000.1a2b3c4d"
    folder = "infra/app"
    pr_number = 7
    keys = build_folder_artifact_keys_for_run(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        pr_number=pr_number,
        pointer_type="report",
    )
    rendered = folder_comment(
        "infra/app",
        _outcome("infra/app"),
        _artifacts(),
        **_report_link_kwargs(
            repo_name=repo_name,
            run_id=run_id,
            pr_number=pr_number,
            existing_names=frozenset({"manifest.json"}),
        ),
    )
    decoded = unquote(rendered)
    assert f"pr-{pr_number}/executions/{run_id}/report/" in decoded
    assert keys.manifest_json in decoded


def test_report_folder_artifact_links_fall_back_to_run_scoped_prefix_without_pr():
    from urllib.parse import unquote

    from src.domain.engine.artifact_paths import build_folder_artifact_keys

    repo_name = "org/repo"
    run_id = "1756419360000.1a2b3c4d"
    folder = "infra/app"
    keys = build_folder_artifact_keys(
        repo_name=repo_name, run_id=run_id, folder_path=folder
    )
    rendered = folder_comment(
        folder,
        _outcome(folder),
        _artifacts(),
        **_report_link_kwargs(
            repo_name=repo_name,
            run_id=run_id,
            existing_names=frozenset({"manifest.json"}),
        ),
    )
    decoded = unquote(rendered)
    assert keys.manifest_json in decoded
    assert "/executions/" not in decoded


def test_report_folder_execution_shows_step_functions_not_codebuild():
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(),
        **_report_link_kwargs(
            console_url="https://console.aws.example/run",
        ),
    )
    assert "[Step Functions execution](https://console.aws.example/run)" in rendered
    assert "CodeBuild" not in rendered


def test_readonly_plan_below_budget_preserves_tail():
    tail_marker = "TAIL_MARKER_UNIQUE_12345"
    plan_body = (
        "Plan: 1 to add, 0 to change, 0 to destroy\n"
        + ("+ resource aws_instance.probe\n" * 500)
        + tail_marker
    )
    assert len(plan_body) < 32_000
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(plan=plan_body),
        **_report_link_kwargs(action="plan"),
    )
    assert "Output truncated" not in rendered
    assert tail_marker in rendered


def test_readonly_plan_over_budget_truncates_once_with_neutral_note():
    plan_body = (
        "Plan: 1 to add, 0 to change, 0 to destroy\n"
        + ("+ resource aws_instance.probe\n" * 20_000)
    )
    assert len(plan_body) > 32_000
    rendered = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(plan=plan_body),
        **_report_link_kwargs(action="plan"),
    )
    assert (
        rendered.count(
            "Output truncated. See S3 artifacts for full plan output."
        )
        == 1
    )
    _assert_plan_truncation_note_outside_fence(rendered)
    assert "> <summary>Security" in rendered
    assert "> <summary>Cost" in rendered
    assert "> <summary>Artifacts</summary>" in rendered
    assert len(re.findall(r"<details\b", rendered)) == rendered.count("</details>")
    assert rendered.count("```") % 2 == 0


def test_bounded_large_report_keeps_balanced_marker_and_artifact_guidance():
    marker = format_comment_object_marker("org/repo", 7, "report", "infra/a")
    body = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(
            plan="Plan: 1 to add, 0 to change, 0 to destroy\n"
            + ("+ resource aws_instance.probe\n" * 20_000)
        ),
        **_report_link_kwargs(),
    )
    rendered = bound_comment(body, max_chars=8_000, suffix=f"\n\n{marker}")
    assert len(rendered) <= 8_000
    assert rendered.endswith(marker)
    assert "Comment truncated for GitHub size limits" in rendered
    assert "> <summary>Artifacts</summary>" in rendered
    assert len(re.findall(r"<details\b", rendered)) == rendered.count("</details>")
    assert rendered.count("```") % 2 == 0


def test_report_delete_and_repost_preserves_report_and_report_all_markers():
    class Client:
        def __init__(self):
            self.store: dict[int, str] = {}
            self.deleted: list[int] = []
            self.next_id = 1

        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, _repo, _pr, tag):
            return [
                (comment_id, "openci-bot")
                for comment_id, body in self.store.items()
                if tag in body
            ]

        def get_comment_body(self, _repo, comment_id):
            return self.store.get(comment_id)

        def delete_comment(self, _repo, comment_id):
            self.deleted.append(comment_id)
            del self.store[comment_id]

        def create_comment(self, _repo, _pr, body):
            comment_id = self.next_id
            self.next_id += 1
            self.store[comment_id] = body
            return comment_id

    client = Client()
    typed_client = cast(GitHubClient, client)
    repo, pr = "org/repo", 7
    folder_marker = format_comment_object_marker(repo, pr, "report", "infra/a")
    summary_marker = format_comment_object_marker(repo, pr, "report-all", "all")

    first_folder_id = render_handler._delete_and_repost(
        typed_client, repo, pr, "folder v1", "report", "infra/a"
    )
    second_folder_id = render_handler._delete_and_repost(
        typed_client, repo, pr, "folder v2", "report", "infra/a"
    )
    first_summary_id = render_handler._delete_and_repost(
        typed_client, repo, pr, "summary v1", "report", "all", report_all=True
    )
    second_summary_id = render_handler._delete_and_repost(
        typed_client, repo, pr, "summary v2", "report", "all", report_all=True
    )

    assert client.deleted == [first_folder_id, first_summary_id]
    assert client.store[second_folder_id].endswith(folder_marker)
    assert client.store[second_summary_id].endswith(summary_marker)
