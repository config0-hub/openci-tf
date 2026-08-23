# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CodeBuild links must identify the hub account required by the AWS console."""

from __future__ import annotations

from src.domain.formatters.artifacts import (
    mutation_status_comment_in_progress,
    mutation_terminal_comment,
)


def test_mutation_progress_labels_codebuild_hub_account() -> None:
    body = mutation_status_comment_in_progress(
        action="apply",
        folder="infra/ec2",
        commit_hash="a" * 40,
        grace_seconds=15,
        console_url="https://example.test/states",
        codebuild_url="https://example.test/codebuild",
        codebuild_account_id="REPLACE_MAIN_ACCOUNT",
        run_id="run-1",
        now=1,
    )

    assert "hub account `REPLACE_MAIN_ACCOUNT`" in body
    assert "switch the AWS console to this account first" in body


def test_mutation_terminal_labels_codebuild_hub_account() -> None:
    body = mutation_terminal_comment(
        action="apply",
        folder="infra/ec2",
        account_id="REPLACE_SECONDARY_ACCOUNT",
        commit_hash="a" * 40,
        succeeded=True,
        pinned_plan_artifact="plan.tfplan",
        console_url="https://example.test/states",
        codebuild_url="https://example.test/codebuild",
        codebuild_account_id="REPLACE_MAIN_ACCOUNT",
        plan_show_text=None,
        plan_show_pointer=None,
    )

    assert "hub account `REPLACE_MAIN_ACCOUNT`" in body
    assert "switch the AWS console to this account first" in body
