# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PR command audit comment formatting."""

from datetime import datetime, timezone

from src.domain.formatters.command_audit import (
    MAX_AUDIT_ROWS,
    append_audit_row,
    command_usage_line,
    format_command_audit_comment,
    format_commands_run_marker,
    parse_audit_rows,
    parse_command_timestamp,
    unsupported_command_help_comment,
)
from src.platform.github.command_audit import record_command_audit

_WHEN = datetime(2026, 8, 18, 10, 3, tzinfo=timezone.utc)


def _append(body, command_text, status="accepted", when=_WHEN):
    return append_audit_row(
        body,
        command_text=command_text,
        status=status,
        when=when,
        repo_name="org/repo",
        pr_number=7,
    )


def test_commands_run_marker_format():
    marker = format_commands_run_marker("org/repo", 29)
    assert marker == "comment_object_id: org/repo:::pr-29::commands-run"


def test_audit_comment_matches_desired_example_shape():
    created = "2026-08-18 10:03 UTC"
    body = format_command_audit_comment(
        created_at=created,
        rows=[
            (created, "tf report", "accepted"),
            ("2026-08-18 10:07 UTC", "tf banana", "not supported"),
        ],
        repo_name="org/repo",
        pr_number=29,
    )
    assert body.startswith("## openci-tf commands")
    assert command_usage_line() in body
    assert f"Created: {created}" in body
    assert "| `tf report` | accepted |" in body
    assert "| `tf banana` | not supported |" in body
    assert "comment_object_id: org/repo:::pr-29::commands-run" in body


def test_append_audit_row_redacts_confirm_tokens():
    when = datetime(2026, 8, 18, 10, 3, tzinfo=timezone.utc)
    body = append_audit_row(
        None,
        command_text="tf destroy confirm deadbeef",
        status="accepted",
        when=when,
        repo_name="org/repo",
        pr_number=7,
    )
    assert "deadbeef" not in body
    assert "confirm <redacted>" in body
    rows = parse_audit_rows(body)
    assert rows[-1][1] == "tf destroy confirm <redacted>"


def test_unsupported_help_comment_is_short():
    help_body = unsupported_command_help_comment()
    assert help_body.startswith("## openci-tf: command not accepted")
    assert "tf plan <folder-or-csv>" in help_body
    assert "tf plan --destroy <folder-or-csv>" in help_body
    assert "tf report" in help_body


def test_command_usage_line_includes_pipeline_forms():
    usage = command_usage_line()
    assert "tf plan pipeline <name>" in usage
    assert "tf apply pipeline <name> step <n>" in usage


def test_backtick_in_command_round_trips_without_losing_rows():
    body = _append(None, "tf report")
    body = _append(body, "tf plan `infra/a`", status="not supported")
    body = _append(body, "tf report")
    rows = parse_audit_rows(body)
    assert [row[1] for row in rows] == ["tf report", "tf plan infra/a", "tf report"]
    assert "| `tf plan infra/a` | not supported |" in body


def test_pipe_in_command_round_trips_as_github_escape():
    body = _append(None, "tf plan  infra/a | cat", status="not supported")
    body = _append(body, "tf report")
    rows = parse_audit_rows(body)
    assert [row[1] for row in rows] == ["tf plan infra/a \\| cat", "tf report"]
    assert "| `tf plan infra/a \\| cat` | not supported |" in body
    # Re-appending a parsed row must not escape the pipe a second time.
    again = _append(body, rows[0][1], status="not supported")
    assert parse_audit_rows(again)[-1][1] == "tf plan infra/a \\| cat"


def test_append_audit_row_keeps_only_newest_rows():
    body = None
    for index in range(MAX_AUDIT_ROWS + 5):
        body = _append(body, f"tf report {index}")
    rows = parse_audit_rows(body)
    assert len(rows) == MAX_AUDIT_ROWS
    assert rows[0][1] == "tf report 5"
    assert rows[-1][1] == f"tf report {MAX_AUDIT_ROWS + 4}"
    assert len(body) < 65_536 // 2


def test_parse_command_timestamp_inverts_format():
    assert parse_command_timestamp("2026-08-18 10:03 UTC") == _WHEN


class _DuplicateAuditClient:
    """Fake client where a concurrent first-ever audit comment already exists."""

    def __init__(self) -> None:
        self.comments: dict[int, str] = {}
        self.deleted: list[int] = []
        self._next_id = 200

    def find_comment_by_tag(self, _repo, _pr, _tag):
        return None

    def find_comments_by_tag(self, _repo, _pr, tag):
        return [cid for cid, body in self.comments.items() if tag in body]

    def get_comment_body(self, _repo, comment_id):
        return self.comments.get(comment_id)

    def create_comment(self, _repo, _pr, body):
        # Simulate the other Lambda invocation winning the race first.
        if not self.comments:
            self.comments[100] = _append(None, "tf plan infra/other")
        self._next_id += 1
        self.comments[self._next_id] = body
        return self._next_id

    def update_comment(self, _repo, comment_id, body):
        self.comments[comment_id] = body

    def delete_comment(self, _repo, comment_id):
        self.deleted.append(comment_id)
        del self.comments[comment_id]


def test_record_command_audit_merges_concurrent_duplicate_comments():
    client = _DuplicateAuditClient()
    kept = record_command_audit(
        client, "org/repo", 7, command_text="tf report", status="accepted", when=_WHEN
    )
    assert kept == 100
    assert client.deleted == [201]
    assert list(client.comments) == [100]
    rows = parse_audit_rows(client.comments[100])
    assert [row[1] for row in rows] == ["tf plan infra/other", "tf report"]
