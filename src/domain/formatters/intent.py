# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render intent confirmation comments."""
from __future__ import annotations

from src.domain.command.grammar import accepted_verbs
from src.domain.intent.models import IntentGateFailure, IntentRecord


def unknown_verb_refusal_comment(verb: str) -> str:
    verbs = ", ".join(accepted_verbs())
    return f"## tf {verb} refused\n\n- Unknown verb `{verb}`. Accepted verbs: {verbs}"


def intent_failure_comment(action: str, failures: list[IntentGateFailure]) -> str:
    lines = [f"## tf {action} refused", ""]
    for failure in failures:
        if failure.folder:
            lines.append(f"- `{failure.folder}`: {failure.message}")
        else:
            lines.append(f"- {failure.message}")
    return "\n".join(lines)


def intent_success_comment(record: IntentRecord, *, plan_summaries: list[str]) -> str:
    lines = [f"## tf {record.action} intent created", ""]
    lines.extend(plan_summaries)
    lines.append("")
    lines.append(f"To proceed within 10 min: `tf {record.action} confirm {record.token}`")
    return "\n".join(lines)
