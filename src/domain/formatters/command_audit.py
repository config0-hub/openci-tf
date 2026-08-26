# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Formatters for durable PR command audit comments and transient rejection help."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from src.domain.command.grammar import accepted_verbs
from src.domain.formatters.artifacts import _redact_confirm_token

_TABLE_HEADER = "| Time | Command | Status |"
_TABLE_SEP = "|------|---------|--------|"
# Command cells are written through sanitize_audit_command, so a cell never
# contains a backtick and every pipe is the GitHub table escape "\\|".
_ROW_RE = re.compile(
    r"^\|\s*(?P<time>[^|]+?)\s*\|\s*`(?P<command>(?:[^`|\\]|\\\||\\(?!\|))+)`\s*\|\s*(?P<status>accepted|not supported)\s*\|\s*$"
)
MAX_AUDIT_ROWS = 200


def sanitize_audit_command(command_text: str) -> str:
    """Collapse whitespace, drop backticks, and escape pipes for a table cell."""
    collapsed = " ".join(command_text.split()).replace("`", "")
    unescaped = collapsed.replace("\\|", "|")
    return unescaped.replace("|", "\\|")


def _usage_fragments() -> list[str]:
    verbs = accepted_verbs()
    fragments: list[str] = []
    if "plan" in verbs:
        fragments.extend(
            [
                "`tf plan <folder-or-csv>`",
                "`tf plan --destroy <folder-or-csv>`",
                "`tf plan pipeline <name>`",
                "`tf plan --destroy pipeline <name>`",
            ]
        )
    if "report" in verbs:
        fragments.append("`tf report`")
    if "apply" in verbs:
        fragments.extend(
            [
                "`tf apply <folder-or-csv>`",
                "`tf apply pipeline <name>`",
                "`tf apply pipeline <name> step <n>`",
            ]
        )
    if "destroy" in verbs:
        fragments.append("`tf destroy <folder-or-csv>`")
    return fragments


def command_usage_line() -> str:
    return " · ".join(_usage_fragments())


def unsupported_command_help_comment() -> str:
    return f"## openci-tf: command not accepted\n\n{command_usage_line()}"


_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M UTC"


def format_command_timestamp(when: datetime | None = None) -> str:
    moment = when or datetime.now(timezone.utc)
    return moment.strftime(_TIMESTAMP_FORMAT)


def parse_command_timestamp(value: str) -> datetime:
    """Inverse of format_command_timestamp; raises ValueError on any other shape."""
    return datetime.strptime(value.strip(), _TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


def format_commands_run_marker(repo_name: str, pr_number: int) -> str:
    if pr_number < 1:
        raise ValueError("pr_number must be positive")
    return f"comment_object_id: {repo_name}:::pr-{pr_number}::commands-run"


def parse_audit_rows(body: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in body.splitlines():
        match = _ROW_RE.match(line.strip())
        if match is None:
            continue
        rows.append((match.group("time").strip(), match.group("command"), match.group("status")))
    return rows


def parse_audit_created_timestamp(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("Created:"):
            return stripped[len("Created:"):].strip()
    return None


def format_command_audit_comment(
    *,
    created_at: str,
    rows: list[tuple[str, str, str]],
    repo_name: str,
    pr_number: int,
) -> str:
    usage = command_usage_line()
    lines = [
        "## openci-tf commands",
        "",
        usage,
        "",
        f"Created: {created_at}",
        "",
        _TABLE_HEADER,
        _TABLE_SEP,
    ]
    for time_value, command_text, status in rows:
        cell = sanitize_audit_command(_redact_confirm_token(command_text))
        if not cell:
            raise ValueError("audit command cell is empty after sanitizing")
        lines.append(f"| {time_value} | `{cell}` | {status} |")
    lines.append("")
    lines.append(format_commands_run_marker(repo_name, pr_number))
    return "\n".join(lines)


def append_audit_row(
    body: str | None,
    *,
    command_text: str,
    status: str,
    when: datetime | None = None,
    repo_name: str,
    pr_number: int,
) -> str:
    timestamp = format_command_timestamp(when)
    if status not in {"accepted", "not supported"}:
        raise ValueError(f"unsupported audit status: {status!r}")
    redacted_command = _redact_confirm_token(command_text.strip().splitlines()[0].strip())
    if body and format_commands_run_marker(repo_name, pr_number) in body:
        created_at = parse_audit_created_timestamp(body) or timestamp
        rows = parse_audit_rows(body)
    else:
        created_at = timestamp
        rows = []
    rows.append((timestamp, redacted_command, status))
    rows = rows[-MAX_AUDIT_ROWS:]
    return format_command_audit_comment(
        created_at=created_at,
        rows=rows,
        repo_name=repo_name,
        pr_number=pr_number,
    )
