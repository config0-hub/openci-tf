# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Formatters for durable PR command audit comments and transient rejection help."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from src.domain.command.grammar import accepted_verbs
from src.domain.formatters.artifacts import _redact_confirm_token

_TABLE_HEADER = "| Time | Command | Status |"
_TABLE_SEP = "|------|---------|--------|"
# Command cells are written through sanitize_audit_command, so a cell never
# contains a backtick and every pipe is the GitHub table escape "\\|".
# The GitHub delivery id rides at the end of the row as a hidden HTML comment so
# a redelivered webhook cannot append the same command twice.
_ROW_RE = re.compile(
    r"^\|\s*(?P<time>[^|]+?)\s*\|\s*`(?P<command>(?:[^`|\\]|\\\||\\(?!\|))+)`\s*\|\s*(?P<status>accepted|not supported)\s*\|"
    r"\s*(?:<!-- d:(?P<delivery>[^\s>]+) -->)?\s*$"
)
MAX_AUDIT_ROWS = 200
MAX_AUDIT_COMMAND_CHARS = 200
# GitHub rejects comment bodies over 65,536 characters; stay well below that.
MAX_AUDIT_BODY_CHARS = 60_000
_TRUNCATION_HASH_CHARS = 12

AuditRow = tuple[str, str, str, str | None]


def bound_audit_command(command_text: str) -> str:
    """Cap a command cell at MAX_AUDIT_COMMAND_CHARS, suffixing a sha256 prefix when cut."""
    if len(command_text) <= MAX_AUDIT_COMMAND_CHARS:
        return command_text
    digest = hashlib.sha256(command_text.encode("utf-8")).hexdigest()[:_TRUNCATION_HASH_CHARS]
    return f"{command_text[:MAX_AUDIT_COMMAND_CHARS]} [truncated sha256:{digest}]"


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
                "`tf drift pipeline <name>`",
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


def parse_audit_rows(body: str) -> list[AuditRow]:
    rows: list[AuditRow] = []
    for line in body.splitlines():
        match = _ROW_RE.match(line.strip())
        if match is None:
            continue
        rows.append(
            (
                match.group("time").strip(),
                match.group("command"),
                match.group("status"),
                match.group("delivery"),
            )
        )
    return rows


def audit_row_exists_for_delivery(body: str | None, delivery_id: str | None) -> bool:
    if not body or not delivery_id:
        return False
    return any(row[3] == delivery_id for row in parse_audit_rows(body))


def parse_audit_created_timestamp(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("Created:"):
            return stripped[len("Created:"):].strip()
    return None


def format_command_audit_comment(
    *,
    created_at: str,
    rows: list[AuditRow],
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
    for time_value, command_text, status, delivery_id in rows:
        cell = sanitize_audit_command(_redact_confirm_token(command_text))
        if not cell:
            raise ValueError("audit command cell is empty after sanitizing")
        suffix = f"<!-- d:{delivery_id} -->" if delivery_id else ""
        lines.append(f"| {time_value} | `{cell}` | {status} |{suffix}")
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
    delivery_id: str | None = None,
) -> str:
    """Append one row; a row already carrying ``delivery_id`` leaves the body unchanged."""
    timestamp = format_command_timestamp(when)
    if status not in {"accepted", "not supported"}:
        raise ValueError(f"unsupported audit status: {status!r}")
    if delivery_id is not None and (not delivery_id or any(ch.isspace() or ch == ">" for ch in delivery_id)):
        raise ValueError(f"invalid audit delivery id: {delivery_id!r}")
    first_line = command_text.strip().splitlines()[0].strip() if command_text.strip() else ""
    redacted_command = bound_audit_command(sanitize_audit_command(_redact_confirm_token(first_line)))
    if body and format_commands_run_marker(repo_name, pr_number) in body:
        if audit_row_exists_for_delivery(body, delivery_id):
            return body
        created_at = parse_audit_created_timestamp(body) or timestamp
        rows = parse_audit_rows(body)
    else:
        created_at = timestamp
        rows = []
    rows.append((timestamp, redacted_command, status, delivery_id))
    rows = rows[-MAX_AUDIT_ROWS:]
    rendered = format_command_audit_comment(
        created_at=created_at, rows=rows, repo_name=repo_name, pr_number=pr_number
    )
    while len(rendered) > MAX_AUDIT_BODY_CHARS and len(rows) > 1:
        rows = rows[1:]
        rendered = format_command_audit_comment(
            created_at=created_at, rows=rows, repo_name=repo_name, pr_number=pr_number
        )
    if len(rendered) > MAX_AUDIT_BODY_CHARS:
        raise ValueError("audit comment exceeds the body limit with a single row")
    return rendered
