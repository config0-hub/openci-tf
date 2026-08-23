"""Authorization-bounded cross-trigger run-list adapter coverage."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.platform.aws.run_registry.keys import (
    repo_gsi_pk,
    repo_gsi_sk,
    run_meta_sk,
    run_pk,
)
from src.platform.aws import run_registry
from src.platform.aws.run_registry._shared import (
    _MAX_RUN_LIST_EVALUATED_ITEMS,
    _MAX_RUN_LIST_EVALUATED_PAGES,
)


def _row(
    trigger_id: str,
    run_id: str,
    created_at: int,
    *,
    repo_name: str,
    action: str = "plan",
) -> dict[str, object]:
    return {
        "pk": run_pk(run_id),
        "sk": run_meta_sk(),
        "gsi1pk": repo_gsi_pk(trigger_id),
        "gsi1sk": repo_gsi_sk(created_at, run_id),
        "run_id": run_id,
        "trigger_id": trigger_id,
        "repo_name": repo_name,
        "action": action,
        "expire_ttl": Decimal(4102444800),
    }


class FakeRunTable:
    def __init__(self, rows_by_partition: dict[str, list[dict[str, object]]]):
        self.rows_by_partition = rows_by_partition
        self.queries: list[dict[str, object]] = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        rows = list(self.rows_by_partition.get(str(values[":pk"]), []))
        before = values.get(":before")
        if isinstance(before, str):
            rows = [row for row in rows if str(row["gsi1sk"]) < before]
        rows.sort(key=lambda row: str(row["gsi1sk"]), reverse=True)
        return {"Items": rows[: int(kwargs["Limit"])]}


class PaginatedRunTable(FakeRunTable):
    def query(self, **kwargs):
        self.queries.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        rows = list(self.rows_by_partition.get(str(values[":pk"]), []))
        before = values.get(":before")
        if isinstance(before, str):
            rows = [row for row in rows if str(row["gsi1sk"]) < before]
        exclusive_start_key = kwargs.get("ExclusiveStartKey")
        if isinstance(exclusive_start_key, dict):
            evaluated = str(exclusive_start_key["gsi1sk"])
            rows = [row for row in rows if str(row["gsi1sk"]) < evaluated]
        rows.sort(key=lambda row: str(row["gsi1sk"]), reverse=True)
        page = rows[: int(kwargs["Limit"])]
        response: dict[str, object] = {"Items": page}
        if len(rows) > len(page):
            boundary = page[-1]
            response["LastEvaluatedKey"] = {
                "pk": boundary["pk"],
                "sk": boundary["sk"],
                "gsi1pk": boundary["gsi1pk"],
                "gsi1sk": boundary["gsi1sk"],
            }
        return response


def test_cross_trigger_list_merges_filters_and_pages_stably(monkeypatch):
    table = FakeRunTable(
        {
            "repo#trigger-a": [
                _row("trigger-a", "a3", 300, repo_name="Acme/Payments"),
                _row("trigger-a", "a2", 200, repo_name="acme/other"),
                _row("trigger-a", "a1", 100, repo_name="acme/payments", action="apply"),
            ],
            "repo#trigger-b": [
                _row("trigger-b", "b4", 400, repo_name="acme/payments", action="drift"),
                _row("trigger-b", "b1", 150, repo_name="acme/payments", action="report"),
            ],
        }
    )
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    first, cursor = run_registry.list_runs_authorized(
        ("trigger-a", "trigger-b"),
        actions=frozenset({"plan", "drift", "report"}),
        repo_filter="PAYMENTS",
        limit=2,
    )
    assert [item["run_id"] for item in first] == ["b4", "a3"]
    assert cursor == repo_gsi_sk(300, "a3")
    assert all(item["action"] != "apply" for item in first)

    second, next_cursor = run_registry.list_runs_authorized(
        ("trigger-a", "trigger-b"),
        actions=frozenset({"plan", "drift", "report"}),
        repo_filter="payments",
        limit=2,
        cursor=cursor,
    )
    assert [item["run_id"] for item in second] == ["b1"]
    assert next_cursor is None
    assert all(":before" in query["ExpressionAttributeValues"] for query in table.queries[-2:])


def test_selective_list_stops_at_page_budget_and_returns_empty_boundary_cursor(monkeypatch):
    rows = [
        _row("trigger-a", f"run-{index:02d}", 1_000 - index, repo_name="acme/other")
        for index in range(40)
    ]
    table = PaginatedRunTable({"repo#trigger-a": rows})
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    page, cursor = run_registry.list_runs_authorized(
        ("trigger-a",),
        actions=frozenset({"plan"}),
        repo_filter="payments",
        limit=1,
    )

    assert page == []
    assert len(table.queries) == _MAX_RUN_LIST_EVALUATED_PAGES
    assert cursor == rows[_MAX_RUN_LIST_EVALUATED_PAGES - 1]["gsi1sk"]

    next_page, next_cursor = run_registry.list_runs_authorized(
        ("trigger-a",),
        actions=frozenset({"plan"}),
        repo_filter="payments",
        limit=1,
        cursor=cursor,
    )
    assert next_page == []
    next_boundary = _MAX_RUN_LIST_EVALUATED_PAGES * 2 - 1
    assert next_cursor == rows[next_boundary]["gsi1sk"]


def test_selective_list_stops_at_evaluated_item_budget(monkeypatch):
    rows = [
        _row("trigger-a", f"run-{index:04d}", 10_000 - index, repo_name="acme/other")
        for index in range(1_000)
    ]
    table = PaginatedRunTable({"repo#trigger-a": rows})
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    page, cursor = run_registry.list_runs_authorized(
        ("trigger-a",),
        actions=frozenset({"plan"}),
        repo_filter="payments",
        limit=100,
    )

    assert page == []
    assert sum(int(query["Limit"]) for query in table.queries) == _MAX_RUN_LIST_EVALUATED_ITEMS
    assert cursor == rows[_MAX_RUN_LIST_EVALUATED_ITEMS - 1]["gsi1sk"]


def test_selective_list_returns_matches_with_evaluated_boundary_cursor(monkeypatch):
    rows = [
        _row(
            "trigger-a",
            f"run-{index:02d}",
            1_000 - index,
            repo_name="acme/payments" if index == 0 else "acme/other",
        )
        for index in range(40)
    ]
    table = PaginatedRunTable({"repo#trigger-a": rows})
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    page, cursor = run_registry.list_runs_authorized(
        ("trigger-a",),
        actions=frozenset({"plan"}),
        repo_filter="payments",
        limit=2,
    )

    assert [item["run_id"] for item in page] == ["run-00"]
    assert len(table.queries) == _MAX_RUN_LIST_EVALUATED_PAGES
    evaluated_items = _MAX_RUN_LIST_EVALUATED_PAGES * 2
    assert cursor == rows[evaluated_items - 1]["gsi1sk"]


@pytest.mark.parametrize("limit", [-1, 0, 1, 100, 101, 10_000])
def test_cross_trigger_list_clamps_limit(monkeypatch, limit: int):
    table = FakeRunTable({"repo#trigger-a": []})
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    run_registry.list_runs_authorized(
        ("trigger-a",),
        actions=frozenset({"plan"}),
        limit=limit,
    )

    assert table.queries[0]["Limit"] == min(max(1, limit), 100)


@pytest.mark.parametrize("cursor", ["forged", "0#run", "9" * 21 + "#run", "0" * 20 + "#bad/run", "x" * 1024])
def test_cross_trigger_list_rejects_forged_or_oversized_cursor_before_io(monkeypatch, cursor: str):
    table = FakeRunTable({"repo#trigger-a": []})
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    with pytest.raises(run_registry.RunRegistryQueryError, match="invalid run cursor"):
        run_registry.list_runs_authorized(
            ("trigger-a",),
            actions=frozenset({"plan"}),
            cursor=cursor,
        )

    assert table.queries == []


def test_cross_trigger_list_propagates_backend_errors(monkeypatch):
    class BrokenTable:
        def query(self, **_kwargs):
            raise RuntimeError("sentinel query outage")

    monkeypatch.setattr(run_registry._shared, "_table", lambda: BrokenTable())

    with pytest.raises(RuntimeError, match="sentinel query outage"):
        run_registry.list_runs_authorized(
            ("trigger-a",),
            actions=frozenset({"plan"}),
        )
