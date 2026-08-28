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
    infracost: str = '{"totalMonthlyCost":"0"}',
) -> dict[str, str]:
    return {
        "init.out": "Terraform has been successfully initialized!",
        "validate.out": "Success! The configuration is valid.",
        "tf/plan.out": plan,
        "tfsec.json": tfsec,
        "infracost.json": infracost,
    }


def test_report_summary_uses_drift_header_and_accessible_labels():
    rendered = summary(
        [_outcome("infra/a")],
        {"infra/a": _artifacts()},
        action="report",
    )
    assert "| Drift |" in rendered
    assert "✅ CLEAN" in rendered
    assert "| Plan |" not in rendered


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
    assert "✅ 2 clean folders" in rendered
    assert rendered.count("<details>") == 1
    clean_section = rendered.split("✅ 2 clean folders", 1)[1]
    assert "`infra/clean`" in clean_section
    assert "`infra/also-clean`" in clean_section
    attention_section = rendered.split("### Needs attention", 1)[1].split(
        "✅ 2 clean folders", 1
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
    assert "All folders clean" in rendered
    assert "### Needs attention" not in rendered
    assert "✅ 2 clean folders" in rendered


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
        action="report",
        commit_hash=_FULL_SHA,
    )
    assert rendered.startswith("<details>")
    inner = rendered.split("</summary>", 1)[1]
    assert inner.count("<details>") >= 3
    plan_region = inner.split("### Plan", 1)[1].split("Security ·", 1)[0]
    assert "```diff" in plan_region
    assert "+ resource aws_instance.example" in plan_region
    assert "- resource aws_instance.old" in plan_region
    assert "### Plan\n<details>" not in rendered


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
        action="report",
    )
    assert "**2 to add**" in rendered
    assert "**3 to change**" in rendered
    assert "**1 to destroy**" in rendered
    assert "+ resource aws_s3_bucket.new" in rendered
    assert "! resource aws_s3_bucket.changed" in rendered
    assert "- resource aws_s3_bucket.old" in rendered


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
                infracost='{"totalMonthlyCost":"12.50"}',
            ),
        },
        action="report",
        commit_hash=_FULL_SHA,
        run_id="run-1",
        repo_name="org/repo",
        pr_number=7,
        manifest_s3_uri="s3://tmp/manifest.json",
        console_url="https://console.aws.example/run",
    )
    assert "⚠️ DRIFT" in rendered.split("</summary>", 1)[0]
    assert "🛑 HIGH" in rendered.split("</summary>", 1)[0]
    assert "<summary>Security · 🛑 HIGH · 2 findings</summary>" in rendered
    assert "<summary>Cost · $12.50/mo</summary>" in rendered
    assert (
        "<summary>TF setup · Init succeeded · Validate succeeded</summary>" in rendered
    )
    assert "<summary>Download and execution artifacts</summary>" in rendered
    assert "Result #1 HIGH bad" in rendered


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
        action="report",
        commit_hash=_FULL_SHA,
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


def test_plan_summary_unchanged_by_report_formatting():
    rendered = summary(
        [_outcome("infra/a")],
        {"infra/a": _artifacts()},
        action="plan",
    )
    assert "## Terraform Multi-Folder Summary" in rendered
    assert "| Plan |" in rendered
    assert "| no changes | clean |" in rendered
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
    assert "❔ UNKNOWN" in rendered
    assert "🛑 HIGH · 1 finding" in rendered


def test_report_summary_skipped_and_pending_are_honest_not_high_risk_alerts():
    rendered = summary(
        [
            _outcome("infra/skipped", status="skipped"),
            _outcome("infra/pending", status="in_progress"),
        ],
        {},
        action="report",
    )
    assert "⏭️ NOT RUN" in rendered
    assert "⏳ PENDING" in rendered
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
    assert "❔ UNKNOWN" in rendered
    assert "All folders clean" not in rendered
    assert "✅ 1 clean folder" not in rendered


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
    assert "🛑 CRITICAL · 2 findings" in rendered


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
            _artifacts(tfsec=payload),
            action="report",
        )
        assert "<summary>Security · ❔ UNKNOWN</summary>" in rendered
        assert "Security data unavailable." in rendered


def test_bounded_large_report_keeps_balanced_marker_and_artifact_guidance():
    marker = format_comment_object_marker("org/repo", 7, "report", "infra/a")
    body = folder_comment(
        "infra/a",
        _outcome("infra/a"),
        _artifacts(
            plan="Plan: 1 to add, 0 to change, 0 to destroy\n"
            + ("+ resource aws_instance.probe\n" * 20_000)
        ),
        action="report",
        commit_hash=_FULL_SHA,
        run_id="run-1",
        repo_name="org/repo",
        pr_number=7,
        manifest_s3_uri="s3://tmp/manifest.json",
        console_url="https://console.aws.example/run",
    )
    rendered = bound_comment(body, max_chars=8_000, suffix=f"\n\n{marker}")
    assert len(rendered) <= 8_000
    assert rendered.endswith(marker)
    assert rendered.count("Comment truncated for GitHub size limits") == 1
    assert "### Download and execution artifacts" in rendered
    assert "Plan pointer" in rendered
    assert "Manifest: `s3://tmp/manifest.json`" in rendered
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
