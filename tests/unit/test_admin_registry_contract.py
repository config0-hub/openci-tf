"""Direct contract coverage for read-only admin registry adapters."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.platform.aws import admin_registry


class FakeTable:
    def __init__(self, rows_by_partition: dict[str, list[dict[str, object]]]):
        self.rows_by_partition = rows_by_partition
        self.queries: list[dict[str, object]] = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        partition = str(kwargs["ExpressionAttributeValues"][":pk"])
        return {"Items": self.rows_by_partition.get(partition, [])}


def _install_table(monkeypatch, table: FakeTable) -> None:
    monkeypatch.setenv("SETTINGS_TABLE_NAME", "settings")
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(admin_registry, "dynamo_table", lambda _name: table)


def test_admin_adapters_strip_sentinel_secrets(monkeypatch):
    sentinel = "SENTINEL-SECRET-MUST-NOT-LEAK"
    table = FakeTable(
        {
            "repo": [
                {
                    "pk": "repo",
                    "sk": "trigger-1",
                    "repo_name": "org/repo",
                    "require_approval": True,
                    "secret": sentinel,
                    "engine_webhook_secret": sentinel,
                    "git_url": f"https://{sentinel}@github.com/org/repo.git",
                }
            ],
            "account": [
                {
                    "pk": "account",
                    "sk": "production",
                    "account_id": "123456789012",
                    "role_name": "openci-tf-executor-remote",
                    "external_id": sentinel,
                    "session_ttl": Decimal(900),
                    "assume_role_arn": f"arn:aws:iam::123456789012:role/{sentinel}",
                }
            ],
            "lock": [
                {
                    "pk": "lock",
                    "sk": "org/repo/infra/vpc",
                    "holder_execution_id": "run.1.0",
                    "expires_at": Decimal(1700000100),
                    "owner_token": sentinel,
                }
            ],
        }
    )
    _install_table(monkeypatch, table)

    repos, _ = admin_registry.list_repo_registrations()
    accounts, _ = admin_registry.list_account_targets()
    locks, _ = admin_registry.list_active_locks(now=1_700_000_000)

    assert repos == [
        {"repo_name": "org/repo", "trigger_ids": ["trigger-1"], "require_approval": True}
    ]
    assert accounts == [
        {
            "alias": "production",
            "account_id": "123456789012",
            "role_name": "openci-tf-executor-remote",
        }
    ]
    assert locks == [
        {
            "repo_name": "org/repo",
            "folder": "infra/vpc",
            "holder_execution_id": "run.1.0",
            "expires_at": 1_700_000_100,
        }
    ]
    assert sentinel not in repr((repos, accounts, locks))


@pytest.mark.parametrize(("requested", "expected"), [(-50, 1), (0, 1), (25, 25), (101, 100), (10_000, 100)])
def test_admin_adapter_clamps_page_limit(monkeypatch, requested: int, expected: int):
    table = FakeTable({"repo": []})
    _install_table(monkeypatch, table)

    admin_registry.list_repo_registrations(limit=requested)

    assert table.queries[0]["Limit"] == expected


def test_forged_admin_cursor_remains_confined_to_route_partition(monkeypatch):
    table = FakeTable({"repo": []})
    _install_table(monkeypatch, table)

    admin_registry.list_repo_registrations(cursor="account/production")

    assert table.queries[0]["ExclusiveStartKey"] == {
        "pk": "repo",
        "sk": "account/production",
    }


def test_oversized_admin_cursor_is_rejected_before_io(monkeypatch):
    table = FakeTable({"repo": []})
    _install_table(monkeypatch, table)

    with pytest.raises(admin_registry.AdminCursorError, match="exceeds maximum"):
        admin_registry.list_repo_registrations(cursor="x" * 513)

    assert table.queries == []


def test_admin_adapter_propagates_backend_errors(monkeypatch):
    class BrokenTable:
        def query(self, **_kwargs):
            raise RuntimeError("sentinel dynamodb outage")

    monkeypatch.setenv("SETTINGS_TABLE_NAME", "settings")
    monkeypatch.setattr(admin_registry, "dynamo_table", lambda _name: BrokenTable())

    with pytest.raises(RuntimeError, match="sentinel dynamodb outage"):
        admin_registry.list_account_targets()
