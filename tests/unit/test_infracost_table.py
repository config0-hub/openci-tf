# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Infracost JSON table rendering and stable comment upserts."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.domain.formatters.artifacts import _MAX_COMMENT_CHARS, bound_comment, folder_comment, infracost
from src.domain.formatters.infracost_table import render_infracost_table
from src.platform.github.client import GitHubClient, generate_search_tag
from src.services.render import handler as render_handler

_FIXTURES = Path("tests/fixtures/artifacts")
_SAMPLE_JSON = _FIXTURES / "infracost_breakdown.json"


def _load_sample() -> str:
    if not _SAMPLE_JSON.exists():
        pytest.skip("infracost breakdown fixture unavailable")
    return _SAMPLE_JSON.read_text()


def test_render_realistic_infracost_json_has_itemized_rows_and_no_raw_json():
    text = _load_sample()
    table = render_infracost_table(text)
    rendered = infracost(text)
    assert "aws_instance.probe" in table
    assert "Instance usage (Linux/UNIX, on-demand, t3.nano)" in table
    assert "root_block_device" in table
    assert "Storage (general purpose SSD, gp3)" in table
    assert "Monthly cost depends on usage" in table
    assert "OVERALL TOTAL" in table
    assert "$5.73" in table
    assert '"projects"' not in rendered
    assert '{"' not in rendered


def test_infracost_section_renders_unavailable_message_on_invalid_json():
    rendered = infracost("{not-json")
    assert "Cost data unavailable (invalid JSON)." in rendered
    assert "Monthly cost" not in rendered
    assert "```" not in rendered


def test_render_skipped_malformed_empty_and_zero_cost_payloads():
    assert "not configured" in render_infracost_table('{"skipped":true,"reason":"not configured"}')
    assert "invalid JSON" in render_infracost_table("{not-json")
    assert "empty breakdown" in render_infracost_table('{"projects":[]}')
    zero = json.dumps({
        "totalMonthlyCost": "0",
        "projects": [{
            "name": ".",
            "breakdown": {
                "resources": [{
                    "name": "aws_s3_bucket.free",
                    "costComponents": [{
                        "name": "Standard storage",
                        "unit": "GB",
                        "monthlyQuantity": "0",
                        "price": "0.023",
                        "monthlyCost": "0",
                    }],
                }],
                "totalMonthlyCost": "0",
            },
        }],
    })
    table = render_infracost_table(zero)
    assert "aws_s3_bucket.free" in table
    assert "$0.00" in table


def test_render_large_payload_truncates_explicitly_but_keeps_totals_and_paid_rows():
    resources = []
    for index in range(80):
        resources.append({
            "name": f"aws_instance.node{index}",
            "costComponents": [{
                "name": "Instance usage",
                "unit": "hours",
                "monthlyQuantity": "730",
                "price": "0.01",
                "monthlyCost": "7.30",
            }],
        })
    payload = json.dumps({
        "totalMonthlyCost": "584.00",
        "projects": [{"name": ".", "breakdown": {"resources": resources, "totalMonthlyCost": "584.00"}}],
    })
    table = render_infracost_table(payload, max_rows=12)
    assert "truncated" in table
    assert "OVERALL TOTAL" in table
    assert "PROJECT TOTAL" in table
    assert "$7.30" in table


def test_render_truncation_never_keeps_orphan_component_rows():
    resources = []
    for index in range(40):
        resources.append({
            "name": f"aws_instance.node{index}",
            "costComponents": [{
                "name": "Instance usage",
                "unit": "hours",
                "monthlyQuantity": "730",
                "price": "0.01",
                "monthlyCost": "7.30",
            }],
        })
    payload = json.dumps({
        "totalMonthlyCost": "292.00",
        "projects": [{"name": ".", "breakdown": {"resources": resources, "totalMonthlyCost": "292.00"}}],
    })
    table = render_infracost_table(payload, max_rows=8)
    for line in table.splitlines():
        if "Instance usage" in line:
            assert "aws_instance." in table.split(line)[0]


def test_github_client_delete_and_repost_deletes_all_duplicates_before_post():
    deleted: list[int] = []
    posted: list[str] = []

    class Session:
        def __init__(self):
            self.store = {101: "old-1", 202: "old-2"}
            self.next_id = 303

        def get(self, url, params=None):
            page = (params or {}).get("page", 1)
            comments = []
            if page == 1:
                comments = [{"id": cid, "body": f"body {cid} #openci-tf:::tag::dup"} for cid in self.store]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: comments)

        def patch(self, url, json=None):
            raise AssertionError("delete_and_repost must not PATCH")

        def delete(self, url):
            cid = int(url.rsplit("/", 1)[-1])
            deleted.append(cid)
            del self.store[cid]
            return SimpleNamespace(raise_for_status=lambda: None)

        def post(self, url, json=None):
            posted.append(json["body"])
            cid = self.next_id
            self.store[cid] = json["body"]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": cid})

    client = GitHubClient("token")
    client.session = Session()
    comment_id = client.delete_and_repost("org/repo", 4, "new body #openci-tf:::tag::dup", "openci-tf:::tag::dup")
    assert comment_id == 303
    assert deleted == [101, 202]
    assert posted == ["new body #openci-tf:::tag::dup"]
    assert client.session.store == {303: "new body #openci-tf:::tag::dup"}


def test_render_repeated_folder_and_summary_delete_and_repost_gets_new_ids(monkeypatch):
    class Session:
        def __init__(self):
            self.store: dict[int, str] = {}
            self.next_id = 5202721251
            self.deleted: list[int] = []

        def post(self, url, json=None):
            cid = self.next_id
            self.next_id += 1
            self.store[cid] = json["body"]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": cid})

        def patch(self, url, json=None):
            raise AssertionError("generated comments must not be PATCHed")

        def delete(self, url):
            cid = int(url.rsplit("/", 1)[-1])
            self.deleted.append(cid)
            del self.store[cid]
            return SimpleNamespace(raise_for_status=lambda: None)

        def get(self, url, params=None):
            page = (params or {}).get("page", 1)
            if page > 1:
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [])
            comments = [
                {"id": cid, "body": body, "user": {"login": "openci-bot"}}
                for cid, body in self.store.items()
            ]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: comments)

    from src.domain.github.comment_object_id import format_comment_object_marker

    session = Session()
    client = GitHubClient("token")
    client.session = session
    client._token_login = "openci-bot"
    repo, pr = "<REPO_ORG>/<REPO_NAME>", 4
    folder_marker = format_comment_object_marker(repo, pr, "plan", "infra/a")
    summary_marker = format_comment_object_marker(repo, pr, "plan", "all")
    folder_id = render_handler._delete_and_repost(
        client, repo, pr, "folder v1", "plan", "infra/a"
    )
    summary_id = render_handler._delete_and_repost(
        client, repo, pr, "summary v1", "plan", "all"
    )
    folder_id_2 = render_handler._delete_and_repost(
        client, repo, pr, "folder v2", "plan", "infra/a"
    )
    summary_id_2 = render_handler._delete_and_repost(
        client, repo, pr, "summary v2", "plan", "all"
    )
    assert folder_id == 5202721251
    assert summary_id == 5202721252
    assert folder_id_2 == 5202721253
    assert summary_id_2 == 5202721254
    assert session.deleted == [folder_id, summary_id]
    assert len(session.store) == 2
    assert session.store[folder_id_2].count(folder_marker) == 1
    assert session.store[summary_id_2].count(summary_marker) == 1


def _assert_balanced_markdown(rendered: str) -> None:
    assert rendered.count("```") % 2 == 0, "unbalanced code fences"
    assert rendered.lower().count("<details>") == rendered.lower().count("</details>"), "unbalanced details tags"


def test_bound_comment_truncation_retains_single_tag_and_cost_totals():
    tag = "#openci-tf:::tag::cost-table"
    table = render_infracost_table(_load_sample())
    body = "## Terraform\n\n### 5 Cost\n```\n" + table + "\n```\n" + ("detail line\n" * 20_000)
    rendered = bound_comment(body, max_chars=8_000, suffix=f"\n\n{tag}")
    assert len(rendered) <= 8_000
    assert rendered.count(tag) == 1
    assert "OVERALL TOTAL" in rendered
    assert "$5.73" in rendered
    assert "truncated" in rendered.lower()
    _assert_balanced_markdown(rendered)


def test_bound_comment_preserves_cost_section_after_huge_plan():
    from src.domain.formatters.artifacts import folder_comment

    tag = "#openci-tf:::tag::folder-infra/a"
    artifacts = {
        "init.out": "Terraform has been successfully initialized!",
        "validate.out": "Success! The configuration is valid.",
        "tf/plan.out": "Plan: 1 to add, 0 to change, 0 to destroy.\n" + ("+ resource aws_instance.probe\n" * 20_000),
        "tfsec.json": '{"results":[]}',
        "infracost.json": _load_sample(),
    }
    body = folder_comment("infra/a", {"folder": "infra/a", "succeeded": True, "account_id": "123456789012"}, artifacts)
    rendered = bound_comment(body, max_chars=8_000, suffix=f"\n\n{tag}")
    assert len(rendered) <= 8_000
    assert rendered.count(tag) == 1
    assert "### Cost Analysis" in rendered
    assert "aws_instance.probe" in rendered
    assert "Instance usage (Linux/UNIX, on-demand, t3.nano)" in rendered
    assert "OVERALL TOTAL" in rendered
    assert "$5.73" in rendered
    assert "truncated" in rendered.lower()
    assert '{"projects"' not in rendered
    _assert_balanced_markdown(rendered)
    assert "### Security Scan" in rendered
    assert "Security Scan Results" in rendered


def test_bound_comment_huge_plan_preserves_realistic_folder_comment_order():
    """Regression: adversarial truncation must not orphan fences or details blocks."""
    tag = "#openci-tf:::tag::folder-infra/a"
    tfsec_json = json.dumps(
        {
            "results": [
                {
                    "severity": "HIGH",
                    "rule_description": "S3 bucket encryption disabled",
                    "location": {"filename": "main.tf", "start_line": 12},
                }
            ]
        }
    )
    artifacts = {
        "init.out": "Terraform has been successfully initialized!",
        "validate.out": "Success! The configuration is valid.",
        "tf/plan.out": "Plan: 1 to add, 0 to change, 0 to destroy.\n" + ("+ resource aws_instance.probe\n" * 20_000),
        "tfsec.json": tfsec_json,
        "infracost.json": _load_sample(),
    }
    body = folder_comment("infra/a", {"folder": "infra/a", "succeeded": True, "account_id": "123456789012"}, artifacts)
    rendered = bound_comment(body, max_chars=8_000, suffix=f"\n\n{tag}")
    assert len(rendered) <= 8_000
    assert rendered.count(tag) == 1
    _assert_balanced_markdown(rendered)
    assert "### Security Scan" in rendered
    assert "S3 bucket encryption disabled" in rendered
    assert "### Cost Analysis" in rendered
    assert "OVERALL TOTAL" in rendered
    assert "$5.73" in rendered
    assert '{"projects"' not in rendered
    assert '{"results"' not in rendered
