# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Formatters for durable PR command audit comments and transient rejection help."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from src.domain.command.grammar import accepted_verbs
from src.domain.formatters.command_text import (
    MAX_COMMAND_CONTEXT_CHARS,
    bound_command_line,
    normalized_command_context_line,
    redact_confirm_token,
    sanitize_command_line,
)
from src.domain.github.comment_object_id import (
    body_has_commands_run_audit_marker,
    find_commands_run_marker,
)

_TABLE_HEADER = "| Time | Command | Status |"
_TABLE_SEP = "|------|---------|--------|"
# Command cells are written through sanitize_audit_command, so a cell never
# contains a backtick and every pipe is the GitHub table escape "\\|".
# The GitHub delivery id rides at the end of the row as a hidden HTML comment so
# a redelivered webhook cannot append the same command twice.
_ROW_RE = re.compile(
    r"^\|\s*(?P<time>[^|]+?)\s*\|\s*`(?P<command>(?:[^`|\\]|\\\||\\(?!\|))+)`\s*\|\s*(?P<status>accepted|not supported)\s*\|"
    r"\s*(?:(?:<!-- d:(?P<delivery>[^\s>]+) -->)|(?:<!-- l:(?P<legacy>[^\s>]+) -->))?\s*$"
)
MAX_AUDIT_ROWS = 200
MAX_AUDIT_COMMAND_CHARS = MAX_COMMAND_CONTEXT_CHARS
# GitHub rejects comment bodies over 65,536 characters; stay well below that.
MAX_AUDIT_BODY_CHARS = 60_000
AuditRow = tuple[str, str, str, str | None] | tuple[str, str, str, str | None, str | None]


def _row_parts(row: AuditRow) -> tuple[str, str, str, str | None, str | None]:
    if len(row) == 4:
        time_value, command_text, status, delivery_id = row
        return time_value, command_text, status, delivery_id, None
    if len(row) == 5:
        time_value, command_text, status, delivery_id, legacy_id = row
        return time_value, command_text, status, delivery_id, legacy_id
    raise ValueError(f"invalid audit row shape: {row!r}")


def _validate_hidden_id(value: str, *, field: str) -> None:
    if not value or any(ch.isspace() or ch == ">" for ch in value):
        raise ValueError(f"invalid audit {field}: {value!r}")


def derive_legacy_audit_row_id(source_comment_id: int, row_position: int) -> str:
    """Build the hidden identity for one migrated legacy audit row."""
    if source_comment_id < 1:
        raise ValueError("source_comment_id must be positive")
    if row_position < 0:
        raise ValueError("row_position must be non-negative")
    source = f"{source_comment_id}\0{row_position}"
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def audit_row_content_identity(row: AuditRow) -> str:
    """Return a stable identity for delivery rows and migrated legacy rows."""
    time_value, command_text, status, delivery_id, legacy_id = _row_parts(row)
    if delivery_id is not None:
        return f"delivery:{delivery_id}"
    if legacy_id is not None:
        return f"legacy:{legacy_id}"
    source = f"{time_value}\0{command_text}\0{status}"
    return f"legacy-content:{hashlib.sha256(source.encode()).hexdigest()[:16]}"


def canonical_audit_rows(rows: list[AuditRow]) -> list[AuditRow]:
    """Keep the first row for each delivery id or legacy content identity."""
    seen: set[str] = set()
    canonical: list[AuditRow] = []
    for row in rows:
        identity = audit_row_content_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        canonical.append(row)
    return canonical


def bound_audit_command(command_text: str) -> str:
    """Cap a command cell at MAX_AUDIT_COMMAND_CHARS, suffixing a sha256 prefix when cut."""
    return bound_command_line(command_text)


def sanitize_audit_command(command_text: str) -> str:
    """Collapse whitespace, drop backticks, and escape pipes for a table cell."""
    return sanitize_command_line(command_text)


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


def is_commands_run_audit_comment(body: str) -> bool:
    return body_has_commands_run_audit_marker(body)


def _is_marker_bearing_audit_comment(body: str) -> bool:
    return is_commands_run_audit_comment(body)


def _is_expected_audit_comment(body: str, *, repo_name: str, pr_number: int) -> bool:
    return find_commands_run_marker(body) == {
        "repo_name": repo_name,
        "pr_number": str(pr_number),
    }


def _audit_table_bounds(body: str) -> tuple[list[str], int, int]:
    lines = body.splitlines()
    marker_index = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()),
        None,
    )
    if marker_index is None:
        raise ValueError("audit comment must end with one marker line")
    created_indexes = [
        index
        for index, line in enumerate(lines[:marker_index])
        if line.strip().startswith("Created:")
    ]
    if len(created_indexes) != 1:
        raise ValueError(
            "marker-bearing audit comment must contain exactly one Created line"
        )
    created_index = created_indexes[0]
    table_region = lines[created_index + 1:marker_index]
    header_indexes = [
        index
        for index, line in enumerate(table_region, start=created_index + 1)
        if line.strip() == _TABLE_HEADER
    ]
    sep_indexes = [
        index
        for index, line in enumerate(table_region, start=created_index + 1)
        if line.strip() == _TABLE_SEP
    ]
    if len(header_indexes) != 1 or len(sep_indexes) != 1:
        raise ValueError("audit comment must contain exactly one audit table")
    header_index = header_indexes[0]
    sep_index = sep_indexes[0]
    if sep_index != header_index + 1:
        raise ValueError("audit table separator must follow the header")
    for line in lines[created_index + 1:header_index]:
        if line.strip():
            raise ValueError("unexpected content before audit table")
    for line in lines[marker_index + 1:]:
        if line.strip():
            raise ValueError("unexpected content after audit marker")
    return lines, sep_index, marker_index


def parse_audit_rows(body: str) -> list[AuditRow]:
    if not _is_marker_bearing_audit_comment(body):
        return []
    lines, sep_index, marker_index = _audit_table_bounds(body)
    rows: list[AuditRow] = []
    in_row_block = True
    for line in lines[sep_index + 1:marker_index]:
        stripped = line.strip()
        if not stripped:
            in_row_block = False
            continue
        if not in_row_block:
            raise ValueError(f"unexpected content after audit table: {stripped}")
        match = _ROW_RE.match(stripped)
        if match is None:
            raise ValueError(f"unparseable audit row: {stripped}")
        rows.append(
            (
                match.group("time").strip(),
                match.group("command"),
                match.group("status"),
                match.group("delivery"),
                match.group("legacy"),
            )
        )
    return rows


def audit_row_exists_for_delivery(body: str | None, delivery_id: str | None) -> bool:
    if not body or not delivery_id:
        return False
    return any(row[3] == delivery_id for row in parse_audit_rows(body))


def audit_delivery_has_status(
    body: str | None, delivery_id: str | None, status: str
) -> bool:
    if not body or not delivery_id:
        return False
    if status not in {"accepted", "not supported"}:
        raise ValueError(f"unsupported audit status: {status!r}")
    return any(
        row_delivery_id == delivery_id and row_status == status
        for _time_value, _command_text, row_status, row_delivery_id, _legacy_id
        in parse_audit_rows(body)
    )


def migrate_legacy_audit_rows(body: str, *, source_comment_id: int) -> list[AuditRow]:
    """Parse rows and assign source-based hidden identities to old rows."""
    migrated: list[AuditRow] = []
    for index, row in enumerate(parse_audit_rows(body)):
        time_value, command_text, status, delivery_id, legacy_id = _row_parts(row)
        if delivery_id is None and legacy_id is None:
            legacy_id = derive_legacy_audit_row_id(source_comment_id, index)
        migrated.append((time_value, command_text, status, delivery_id, legacy_id))
    return migrated


def update_audit_row_status(
    body: str | None,
    *,
    delivery_id: str,
    status: str,
    repo_name: str,
    pr_number: int,
) -> str | None:
    """Update an existing audit row by delivery id without appending a new row."""
    if body is None:
        return None
    if status not in {"accepted", "not supported"}:
        raise ValueError(f"unsupported audit status: {status!r}")
    _validate_hidden_id(delivery_id, field="delivery id")
    if not _is_expected_audit_comment(body, repo_name=repo_name, pr_number=pr_number):
        return body
    created_at = parse_audit_created_timestamp(body)
    rows: list[AuditRow] = []
    changed = False
    for row in parse_audit_rows(body):
        time_value, command_text, row_status, row_delivery_id, legacy_id = _row_parts(row)
        if row_delivery_id == delivery_id and row_status != status:
            rows.append((time_value, command_text, status, row_delivery_id, legacy_id))
            changed = True
        else:
            rows.append(row)
    if not changed:
        return body
    rows = rows[-MAX_AUDIT_ROWS:]
    rendered = format_command_audit_comment(
        created_at=created_at or "",
        rows=rows,
        repo_name=repo_name,
        pr_number=pr_number,
    )
    while len(rendered) > MAX_AUDIT_BODY_CHARS and len(rows) > 1:
        rows = rows[1:]
        rendered = format_command_audit_comment(
            created_at=created_at or "",
            rows=rows,
            repo_name=repo_name,
            pr_number=pr_number,
        )
    if len(rendered) > MAX_AUDIT_BODY_CHARS:
        raise ValueError("audit comment exceeds the body limit with a single row")
    return rendered


def parse_audit_created_timestamp(body: str) -> str | None:
    created_values: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("Created:"):
            created_values.append(stripped[len("Created:"):].strip())
    if not _is_marker_bearing_audit_comment(body):
        return created_values[0] if created_values else None
    if len(created_values) != 1:
        raise ValueError(
            "marker-bearing audit comment must contain exactly one Created line"
        )
    return created_values[0]


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
    for row in rows:
        time_value, command_text, status, delivery_id, legacy_id = _row_parts(row)
        cell = sanitize_audit_command(redact_confirm_token(command_text))
        if not cell:
            raise ValueError("audit command cell is empty after sanitizing")
        if delivery_id:
            suffix = f"<!-- d:{delivery_id} -->"
        elif legacy_id:
            suffix = f"<!-- l:{legacy_id} -->"
        else:
            suffix = ""
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
    if delivery_id is not None:
        _validate_hidden_id(delivery_id, field="delivery id")
    redacted_command = normalized_command_context_line(command_text)
    if body and _is_expected_audit_comment(body, repo_name=repo_name, pr_number=pr_number):
        created_at = parse_audit_created_timestamp(body)
        if audit_row_exists_for_delivery(body, delivery_id):
            return body
        rows = parse_audit_rows(body)
    else:
        created_at = timestamp
        rows = []
    rows.append((timestamp, redacted_command, status, delivery_id, None))
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
