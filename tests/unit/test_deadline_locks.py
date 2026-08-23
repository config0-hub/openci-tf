"""Absolute deadline and durable run-lock ownership contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.errors import (
    BudgetUnmintableError,
    DeadlineExceededError,
    PollDeadlineExceededError,
)
from src.domain.accounts.budget import compute_ttl, default_budget_for_action
from src.domain.deadlines import (
    compute_deadline_at,
    deadline_epoch,
    remaining_seconds,
)
from src.domain.engine.prepare import prepare_and_submit as prepare_package_and_submit
from src.domain.locks import run_lock
from src.platform.aws import run_registry
from src.services.orchestration import finalize_run
from src.services.run_folder import poll_done, prepare_and_submit
from tests.helpers.rendered_run_folder_asl import (
    load_rendered_run_folder_definition,
)


class _DurableLockTable:
    name = "locks"

    def __init__(self) -> None:
        self.meta = SimpleNamespace(client=object())
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.deleted: list[dict[str, str]] = []

    def get_item(self, *, Key, **_kwargs):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item is not None else {}

    def delete_item(self, *, Key, **_kwargs):
        self.deleted.append(Key)
        self.items.pop((Key["pk"], Key["sk"]), None)
        return {}

    def query(self, **kwargs):
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        return {
            "Items": [item for (item_pk, _sk), item in self.items.items() if item_pk == pk]
        }


def test_deadline_is_computed_once_and_never_extends_across_consumers() -> None:
    deadline_at = compute_deadline_at(
        "apply",
        [(600, 15), (900, 60)],
        resolved_at=1_700_000_000,
    )
    assert deadline_epoch(deadline_at) == 1_700_001_575
    prepare_budget = remaining_seconds(
        deadline_at, now=1_700_000_100, cap_seconds=900
    )
    poll_budget = remaining_seconds(deadline_at, now=1_700_000_700)
    closer_budget = remaining_seconds(deadline_at, now=1_700_001_000)
    assert prepare_budget == 900
    assert poll_budget == 875
    assert closer_budget == 575
    assert prepare_budget > poll_budget > closer_budget


def test_engine_submission_precheck_runs_after_upload_before_submit() -> None:
    order: list[str] = []
    prepare_package_and_submit(
        payload={"trigger_id": "run"},
        secrets={},
        encrypt=lambda path: order.append("encrypt") or path,
        package=lambda path: order.append("package") or path,
        upload=lambda _path: order.append("upload"),
        pre_submit=lambda: order.append("deadline-precheck"),
        submit=lambda _payload: order.append("submit"),
    )
    assert order == [
        "encrypt",
        "package",
        "upload",
        "deadline-precheck",
        "submit",
    ]


def test_submission_and_poll_refuse_an_expired_absolute_deadline() -> None:
    event = {
        "action": "plan",
        "attempt": 0,
        "budget": 900,
        "deadline_at": "2000-01-01T00:00:00Z",
    }
    with pytest.raises(DeadlineExceededError, match="deadline"):
        prepare_and_submit.handler(event, object())
    probe_event = dict(
        event,
        exec_id="e" * 32,
        submitted_at=1_700_000_000.0,
        done_baseline_version_id=None,
    )
    probe = poll_done.handler(probe_event, object())
    assert probe["probe_status"] == "expired"


def test_deadline_is_plumbed_through_each_physical_lane_consumer() -> None:
    read_outer = Path(
        "infra/deploy/modules/openci_tf/step_function.tf"
    ).read_text(encoding="utf-8")
    mutation_outers = Path(
        "infra/deploy/modules/openci_tf/step_function_mutation_outer.tf"
    ).read_text(encoding="utf-8")
    assert read_outer.count('"deadline_at.$"') >= 2
    assert mutation_outers.count('"deadline_at.$"') >= 3

    states = load_rendered_run_folder_definition()["States"]
    assert "ProbeDone" in states and "WaitBeforeProbe" in states
    for name, state in states.items():
        parameters = state.get("Parameters")
        if isinstance(parameters, dict) and "run_id.$" in parameters:
            assert parameters.get("deadline_at.$") == "$.deadline_at", name


def test_maximum_accepted_timeout_exceeding_role_lifetime_is_rejected() -> None:
    accepted_parser_budget = default_budget_for_action("plan", 3600)
    assert accepted_parser_budget == 3740
    with pytest.raises(BudgetUnmintableError, match="credential horizon"):
        compute_ttl(accepted_parser_budget, 3600)


def test_lock_lease_covers_max_grace_and_fifty_folder_sequential_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_700_000_000
    windows = [(3600, 3600)] * 50
    deadline_at = compute_deadline_at("destroy", windows, resolved_at=now)
    assert deadline_epoch(deadline_at) - now == 50 * (3600 + 3600)
    table = _DurableLockTable()
    captured: list[dict] = []

    def transact(_client, *, transact_items):
        captured.extend(transact_items)

    monkeypatch.setattr(run_lock, "transact_write_items", transact)
    run_lock.acquire(
        table,
        "org/repo",
        "infra/00",
        "exec-0",
        now,
        deadline_epoch(deadline_at) - now,
        "run-0",
        deadline_at,
    )
    lock_item = captured[0]["Put"]["Item"]
    ownership_item = captured[1]["Put"]["Item"]
    assert lock_item["expires_at"] >= deadline_epoch(deadline_at)
    assert ownership_item["expires_at"] == lock_item["expires_at"]
    assert ownership_item["deadline_at"] == deadline_at


def test_abnormal_closer_uses_durable_index_with_empty_corrupt_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = _DurableLockTable()
    lock = {
        "pk": "lock",
        "sk": "org/repo/infra/a",
        "holder_execution_id": "exec-a",
        "holder_run_id": "run-a",
        "deadline_at": "2999-01-01T00:00:00Z",
        "expires_at": 9_999_999_999,
    }
    ownership = {
        "pk": "run-locks#run-a",
        "sk": "lock#org/repo/infra/a",
        "run_id": "run-a",
        "repo": "org/repo",
        "folder": "infra/a",
        "execution_id": "exec-a",
        "deadline_at": "2999-01-01T00:00:00Z",
        "expires_at": 9_999_999_999,
    }
    table.items[(lock["pk"], lock["sk"])] = lock
    table.items[(ownership["pk"], ownership["sk"])] = ownership
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.delenv("RUN_REGISTRY_TABLE_NAME", raising=False)
    monkeypatch.setattr(finalize_run, "dynamo_table", lambda _name: table)

    result = finalize_run.handler(
        {
            "detail": {
                "status": "TIMED_OUT",
                "executionArn": "arn:aws:states:us-east-1:111111111111:execution:openci-tf:run-a",
            },
            "webhook_info": "corrupt-envelope",
            "map_items": ["also-corrupt"],
            "outcomes": None,
        },
        object(),
    )
    assert result == {"finalized": True}
    assert table.items == {}


def test_deadline_persists_with_run_and_each_folder_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_at = "2999-01-01T00:00:00Z"
    table = SimpleNamespace(name="registry")
    updates: list[dict] = []
    writes: list[dict] = []
    table.update_item = lambda **kwargs: updates.append(kwargs)
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)
    monkeypatch.setattr(run_registry._shared, "dynamo_client", lambda: object())
    monkeypatch.setattr(
        run_registry._shared,
        "transact_write_items",
        lambda _client, *, transact_items: writes.extend(transact_items),
    )
    run_registry.set_run_deadline("run-a", deadline_at)
    run_registry.put_folder_attempt(
        run_id="run-a",
        folder="infra/a",
        account_id="123456789012",
        execution_id="exec-a",
        attempt=0,
        status="failed",
        deadline_at=deadline_at,
    )
    assert updates[0]["ExpressionAttributeValues"][":deadline"] == deadline_at
    assert writes[0]["Put"]["Item"]["deadline_at"] == deadline_at
    assert writes[1]["Update"]["ExpressionAttributeValues"][":deadline"] == deadline_at


def test_duplicate_release_is_idempotent_after_durable_cleanup() -> None:
    table = _DurableLockTable()
    assert run_lock.release_all(table, "already-closed") == 0
    assert run_lock.release_all(table, "already-closed") == 0
