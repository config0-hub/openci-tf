# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve PR-scoped context from run registry records."""

from __future__ import annotations

from typing import Any


def github_pr_number(notification_target: object) -> int | None:
    if not isinstance(notification_target, dict):
        return None
    if notification_target.get("type") != "github_pr":
        return None
    pr_number = notification_target.get("pr_number")
    if isinstance(pr_number, int) and not isinstance(pr_number, bool) and pr_number > 0:
        return pr_number
    return None


def pr_number_from_run_record(run: dict[str, Any] | None) -> int | None:
    if not run:
        return None
    return github_pr_number(run.get("notification_target"))
