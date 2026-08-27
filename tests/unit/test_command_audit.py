# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for PR command audit comment formatting."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from src.core.errors import LockHeldError
from src.domain.formatters.command_audit import (
    MAX_AUDIT_BODY_CHARS,
    MAX_AUDIT_COMMAND_CHARS,
    MAX_AUDIT_ROWS,
    append_audit_row,
    command_usage_line,
    format_command_audit_comment,
    format_commands_run_marker,
    parse_audit_rows,
    parse_command_timestamp,
    unsupported_command_help_comment,
)
from src.platform.aws import audit_lock
from src.platform.github import command_audit as audit_module
from src.platform.github.command_audit import record_command_audit
from tests.helpers.fake_locks_table import FakeLocksTable

_WHEN = datetime(2026, 8, 18, 10, 3, tzinfo=timezone.utc)


def _append(body, command_text, status="accepted", when=_WHEN, delivery_id=None):
    return append_audit_row(
        body,
        command_text=command_text,
        status=status,
        when=when,
        repo_name="org/repo",
        pr_number=7,
        delivery_id=delivery_id,
    )


def test_commands_run_marker_format():
    marker = format_commands_run_marker("org/repo", 29)
    assert marker == "comment_object_id: org/repo:::pr-29::commands-run"


def test_audit_comment_matches_desired_example_shape():
    created = "2026-08-18 10:03 UTC"
    body = format_command_audit_comment(
        created_at=created,
        rows=[
            (created, "tf report", "accepted", "d-1"),
            ("2026-08-18 10:07 UTC", "tf banana", "not supported", None),
        ],
        repo_name="org/repo",
        pr_number=29,
    )
    assert body.startswith("## openci-tf commands")
    assert command_usage_line() in body
    assert f"Created: {created}" in body
    assert "| `tf report` | accepted |<!-- d:d-1 -->" in body
    assert "| `tf banana` | not supported |\n" in body
    assert "comment_object_id: org/repo:::pr-29::commands-run" in body
    assert [row[3] for row in parse_audit_rows(body)] == ["d-1", None]


def test_append_audit_row_collapses_multiline_commands():
    body = _append(None, "tf plan\ninfra/a", status="not supported")
    assert "| `tf plan infra/a` | not supported |" in body
    assert parse_audit_rows(body)[-1][1] == "tf plan infra/a"


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


def test_append_audit_row_fails_loud_on_malformed_existing_row():
    body = _append(None, "tf report", delivery_id="guid-1")
    malformed = body.replace("| `tf report` | accepted |", "| `tf report` | ACCEPTED |")
    with pytest.raises(ValueError, match="unparseable audit row"):
        _append(malformed, "tf plan infra/a", delivery_id="guid-2")
    assert "ACCEPTED" in malformed


def test_append_audit_row_skips_duplicate_delivery_id():
    body = _append(None, "tf plan infra/a", delivery_id="guid-1")
    again = _append(body, "tf plan infra/a", delivery_id="guid-1")
    assert again == body
    assert len(parse_audit_rows(again)) == 1
    other = _append(again, "tf plan infra/a", delivery_id="guid-2")
    assert len(parse_audit_rows(other)) == 2


def test_append_audit_row_bounds_oversized_command():
    huge = "tf plan " + "a" * 65_536
    body = _append(None, huge, status="not supported", delivery_id="guid-1")
    assert len(body) < MAX_AUDIT_BODY_CHARS
    rows = parse_audit_rows(body)
    assert len(rows) == 1
    cell = rows[0][1]
    assert cell.startswith(huge[:MAX_AUDIT_COMMAND_CHARS])
    assert " [truncated sha256:" in cell
    assert len(cell) < MAX_AUDIT_COMMAND_CHARS + 40


def test_append_audit_row_keeps_total_body_under_limit():
    body = None
    for index in range(MAX_AUDIT_ROWS):
        body = _append(body, f"tf plan {'x' * 250} {index}", delivery_id=f"{index:036d}")
    assert len(body) <= MAX_AUDIT_BODY_CHARS
    rows = parse_audit_rows(body)
    assert rows[-1][3] == f"{MAX_AUDIT_ROWS - 1:036d}"
    assert len(rows) < MAX_AUDIT_ROWS


class _SimpleAuditClient:
    def __init__(self) -> None:
        self.comments: dict[int, str] = {}
        self.updates = 0

    def token_login(self):
        return "openci-bot"

    def find_comment_by_tag(self, _repo, _pr, tag):
        return next((cid for cid, body in self.comments.items() if tag in body), None)

    def find_comments_by_tag(self, _repo, _pr, tag):
        return [cid for cid, body in self.comments.items() if tag in body]

    def find_comments_by_body_substring(self, _repo, _pr, needle):
        return [
            (cid, "openci-bot")
            for cid, body in self.comments.items()
            if needle in body
        ]

    def get_comment_body(self, _repo, comment_id):
        return self.comments.get(comment_id)

    def create_comment(self, _repo, _pr, body):
        self.comments[100] = body
        return 100

    def update_comment(self, _repo, comment_id, body):
        self.updates += 1
        self.comments[comment_id] = body

    def delete_comment(self, _repo, comment_id):
        del self.comments[comment_id]


def test_record_command_audit_is_idempotent_per_delivery_and_releases_lock():
    client = _SimpleAuditClient()
    table = FakeLocksTable()
    for _ in range(2):
        record_command_audit(
            client, "org/repo", 7, command_text="tf plan infra/a", status="accepted",
            delivery_id="guid-1", lock_table=table, when=_WHEN,
        )
    assert [row[1] for row in parse_audit_rows(client.comments[100])] == ["tf plan infra/a"]
    assert client.updates == 0
    lock_item = table.items[("audit-lock", "org/repo#pr-7")]
    assert lock_item["expires_at"] == 0
    assert "holder" not in lock_item
    assert lock_item["version"] >= 2


def test_audit_lock_accepts_integral_decimal_versions_and_rejects_fractional():
    class DecimalVersionLocksTable(FakeLocksTable):
        def update_item(self, **kwargs):
            response = super().update_item(**kwargs)
            attributes = response.get("Attributes")
            if isinstance(attributes, dict) and "version" in attributes:
                attributes["version"] = Decimal(str(attributes["version"]))
            return response

    table = DecimalVersionLocksTable()
    version = audit_lock.acquire(table, "org/repo", 7, "holder", now=10)
    assert version == 1
    fenced = audit_lock.fence(table, "org/repo", 7, "holder", version, now=11)
    assert fenced == 2

    with pytest.raises(audit_lock.AuditLockVersionError, match="no integer version"):
        audit_lock._version({"version": Decimal("1.5")}, "org/repo", 7)


def test_record_command_audit_rereads_after_fence_failure(monkeypatch):
    client = _SimpleAuditClient()
    client.comments[100] = _append(None, "tf plan old", delivery_id="old")
    table = FakeLocksTable()

    class FenceFailOnceTable(FakeLocksTable):
        def __init__(self) -> None:
            super().__init__()
            self.failed_fence = False

        def update_item(self, **kwargs):
            values = kwargs.get("ExpressionAttributeValues") or {}
            if ":version" in values and not self.failed_fence:
                self.failed_fence = True
                client.comments[100] = _append(client.comments[100], "tf plan infra/b", delivery_id="b")
                item = self.items[("audit-lock", "org/repo#pr-7")]
                item["version"] += 1
                item["expires_at"] = 0
                item.pop("holder", None)
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
            return super().update_item(**kwargs)

    table = FenceFailOnceTable()
    record_command_audit(
        client,
        "org/repo",
        7,
        command_text="tf plan infra/a",
        status="accepted",
        delivery_id="a",
        lock_table=table,
        when=_WHEN,
    )

    rows = parse_audit_rows(client.comments[100])
    assert [row[3] for row in rows] == ["old", "b", "a"]


def test_record_command_audit_retries_then_fails_on_lock_contention(monkeypatch):
    client = _SimpleAuditClient()
    table = FakeLocksTable()
    table.items[("audit-lock", "org/repo#pr-7")] = {"holder": "other", "expires_at": 10**12}
    slept: list[float] = []
    monkeypatch.setattr(audit_module.time, "sleep", lambda seconds: slept.append(seconds))
    with pytest.raises(LockHeldError):
        record_command_audit(
            client, "org/repo", 7, command_text="tf report", status="accepted",
            delivery_id="guid-1", lock_table=table,
        )
    assert 4.0 <= sum(slept) <= 6.0
    assert client.comments == {}


def test_record_command_audit_propagates_non_condition_dynamo_errors():
    class BrokenTable:
        def update_item(self, **_kwargs):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")

    with pytest.raises(ClientError):
        record_command_audit(
            _SimpleAuditClient(), "org/repo", 7, command_text="tf report", status="accepted",
            delivery_id="guid-1", lock_table=BrokenTable(),
        )


class _DuplicateAuditClient:
    """Fake client where a concurrent first-ever audit comment already exists."""

    def __init__(self) -> None:
        self.comments: dict[int, str] = {}
        self.deleted: list[int] = []
        self._next_id = 200

    def token_login(self):
        return "openci-bot"

    def find_comment_by_tag(self, _repo, _pr, _tag):
        return None

    def find_comments_by_tag(self, _repo, _pr, tag):
        return [cid for cid, body in self.comments.items() if tag in body]

    def find_comments_by_body_substring(self, _repo, _pr, needle):
        return [
            (cid, "openci-bot")
            for cid, body in self.comments.items()
            if needle in body
        ]

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
        client, "org/repo", 7, command_text="tf report", status="accepted", when=_WHEN,
        delivery_id="guid-1", lock_table=FakeLocksTable(),
    )
    assert kept == 100
    assert client.deleted == [201]
    assert list(client.comments) == [100]
    rows = parse_audit_rows(client.comments[100])
    assert [row[1] for row in rows] == ["tf plan infra/other", "tf report"]


class _HumanMarkerAuditClient:
    def __init__(self) -> None:
        marker = format_commands_run_marker("org/repo", 7)
        self.comments: dict[int, str] = {
            100: _append(None, "tf plan infra/old"),
            101: f"human note quoting {marker}",
        }
        self.authors = {100: "openci-bot", 101: "alice"}
        self.deleted: list[int] = []
        self.updated: list[int] = []

    def token_login(self):
        return "openci-bot"

    def find_comment_by_tag(self, _repo, _pr, tag):
        return next((cid for cid, body in self.comments.items() if tag in body), None)

    def find_comments_by_tag(self, _repo, _pr, tag):
        return [cid for cid, body in self.comments.items() if tag in body]

    def find_comments_by_body_substring(self, _repo, _pr, needle):
        return [
            (cid, self.authors[cid])
            for cid, body in self.comments.items()
            if needle in body
        ]

    def get_comment_body(self, _repo, comment_id):
        return self.comments.get(comment_id)

    def create_comment(self, _repo, _pr, body):
        self.comments[102] = body
        self.authors[102] = "openci-bot"
        return 102

    def update_comment(self, _repo, comment_id, body):
        self.updated.append(comment_id)
        self.comments[comment_id] = body

    def delete_comment(self, _repo, comment_id):
        self.deleted.append(comment_id)
        del self.comments[comment_id]


def test_record_command_audit_ignores_human_marker_comments():
    client = _HumanMarkerAuditClient()
    kept = record_command_audit(
        client,
        "org/repo",
        7,
        command_text="tf report",
        status="accepted",
        delivery_id="guid-1",
        lock_table=FakeLocksTable(),
        when=_WHEN,
    )
    assert kept == 100
    assert client.deleted == []
    assert client.updated == [100]
    assert 101 in client.comments
    assert "tf report" in client.comments[100]
    assert "tf report" not in client.comments[101]
