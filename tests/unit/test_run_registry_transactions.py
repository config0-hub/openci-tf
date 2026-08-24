# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transaction adapter tests for run registry idempotency claims."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.platform.aws.run_registry.keys import (
    folder_attempt_sk,
    pipeline_apply_gsi_pk,
    run_meta_sk,
    run_pk,
)
from src.platform.aws.run_registry import (
    IdempotencyConflictError,
    claim_idempotent_run,
    expire_ttl,
    find_latest_successful_pipeline_apply,
    mark_pipeline_apply_succeeded,
    put_folder_attempt,
    set_run_pipeline_metadata,
)


def _run_record(run_id: str = "run-1") -> dict:
    created = 1_700_000_000
    return {
        "pk": run_pk(run_id),
        "sk": run_meta_sk(),
        "run_id": run_id,
        "trigger_id": "trigger-1",
        "status": "accepted",
        "status_rank": 0,
        "created_at": created,
        "updated_at": created,
        "expire_ttl": expire_ttl(created),
        "request_fingerprint": "fp-1",
        "idempotency_key": "delivery-1",
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "action": "plan",
        "ingress_source": "github",
        "notification_target": {"type": "registry"},
    }


@patch("src.platform.aws.run_registry.runs.get_idempotency", return_value=None)
@patch("src.platform.aws.dynamo_transactions.dynamo_client")
@patch("src.platform.aws.run_registry._shared._table")
def test_claim_idempotent_run_executes_transaction(mock_table, mock_client, _mock_get):
    table = MagicMock()
    table.name = "registry"
    client = MagicMock()
    mock_table.return_value = table
    mock_client.return_value = client

    run_id, created = claim_idempotent_run(
        "trigger-1",
        "delivery-1",
        request_fingerprint="fp-1",
        run_record=_run_record(),
    )

    assert created is True
    assert run_id == "run-1"
    client.transact_write_items.assert_called_once()
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    assert len(items) == 2
    assert set(items[0]["Put"]["ExpressionAttributeValues"][":now"]) == {"N"}
    assert items[0]["Put"]["TableName"] == "registry"
    assert items[1]["Put"]["ConditionExpression"] == "attribute_not_exists(pk)"


@patch("src.platform.aws.run_registry.runs.get_idempotency")
@patch("src.platform.aws.dynamo_transactions.dynamo_client")
@patch("src.platform.aws.run_registry._shared._table")
def test_claim_idempotent_run_returns_existing_on_race(mock_table, mock_client, mock_get):
    table = MagicMock()
    table.name = "registry"
    client = MagicMock()
    client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}},
        "TransactWriteItems",
    )
    mock_table.return_value = table
    mock_client.return_value = client
    mock_get.side_effect = [None, {"run_id": "existing-run", "request_fingerprint": "fp-1"}]

    run_id, created = claim_idempotent_run(
        "trigger-1",
        "delivery-1",
        request_fingerprint="fp-1",
        run_record=_run_record("new-run"),
    )

    assert created is False
    assert run_id == "existing-run"


@patch("src.platform.aws.run_registry.runs.get_idempotency")
def test_claim_idempotent_run_rejects_conflicting_fingerprint(mock_get):
    mock_get.return_value = {"run_id": "existing-run", "request_fingerprint": "other-fp"}
    with pytest.raises(IdempotencyConflictError):
        claim_idempotent_run(
            "trigger-1",
            "delivery-1",
            request_fingerprint="fp-1",
            run_record=_run_record(),
        )


@patch("src.platform.aws.run_registry.runs.get_idempotency", return_value=None)
@patch("src.platform.aws.dynamo_transactions.dynamo_client")
@patch("src.platform.aws.run_registry._shared._table")
def test_claim_idempotent_run_allows_expired_replacement(mock_table, mock_client, _mock_get):
    table = MagicMock()
    table.name = "registry"
    client = MagicMock()
    mock_table.return_value = table
    mock_client.return_value = client

    claim_idempotent_run(
        "trigger-1",
        "delivery-1",
        request_fingerprint="fp-1",
        run_record=_run_record("fresh-run"),
    )

    first_put = client.transact_write_items.call_args.kwargs["TransactItems"][0]["Put"]
    assert "expire_ttl <= :now" in first_put["ConditionExpression"]


@patch("src.platform.aws.run_registry.runs.get_idempotency")
@patch("src.platform.aws.dynamo_transactions.dynamo_client")
@patch("src.platform.aws.run_registry._shared._table")
def test_claim_idempotent_run_propagates_unexpected_transaction_errors(mock_table, mock_client, mock_get):
    table = MagicMock()
    table.name = "registry"
    client = MagicMock()
    client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}},
        "TransactWriteItems",
    )
    mock_table.return_value = table
    mock_client.return_value = client
    mock_get.return_value = None

    with pytest.raises(ClientError):
        claim_idempotent_run(
            "trigger-1",
            "delivery-1",
            request_fingerprint="fp-1",
            run_record=_run_record(),
        )


@patch("src.platform.aws.run_registry._shared._table")
def test_set_run_pipeline_metadata_is_idempotent_and_guarded(mock_table):
    table = MagicMock()
    mock_table.return_value = table

    set_run_pipeline_metadata("run-1", pipeline="data/primary", step_count=2)

    update = table.update_item.call_args.kwargs
    assert "pipeline = :pipeline" in update["UpdateExpression"]
    assert "step_count = :step_count" in update["UpdateExpression"]
    assert "pipeline = :pipeline" in update["ConditionExpression"]
    assert "step_count = :step_count" in update["ConditionExpression"]
    assert update["ExpressionAttributeValues"][":pipeline"] == "data/primary"
    assert update["ExpressionAttributeValues"][":step_count"] == 2


@pytest.mark.parametrize("step_count", [0, True, 21])
def test_set_run_pipeline_metadata_rejects_invalid_step_count(step_count):
    with pytest.raises(ValueError, match="step_count"):
        set_run_pipeline_metadata("run-1", pipeline="data/primary", step_count=step_count)


@patch("src.platform.aws.run_registry._shared._table")
def test_mark_pipeline_apply_succeeded_writes_queryable_step_index(mock_table):
    table = MagicMock()
    mock_table.return_value = table

    mark_pipeline_apply_succeeded(
        "run-1",
        trigger_id="trigger-1",
        repo_name="org/repo",
        pipeline="data/primary",
        step_index=2,
        step_count=3,
        pipeline_sha256="c" * 64,
        completed_at=1_700_000_000,
    )

    update = table.update_item.call_args.kwargs
    assert "gsi2pk = :gsi2pk" in update["UpdateExpression"]
    assert "gsi2sk = :gsi2sk" in update["UpdateExpression"]
    assert "#status = :succeeded" in update["ConditionExpression"]
    assert update["ExpressionAttributeValues"][":step_index"] == 2
    assert update["ExpressionAttributeValues"][":pipeline_sha256"] == "c" * 64


@pytest.mark.parametrize(
    ("step_index", "step_count"),
    [(21, 21), (20, 21), (3, 2)],
)
def test_mark_pipeline_apply_succeeded_rejects_out_of_bounds_steps(step_index, step_count):
    with pytest.raises(ValueError, match="step_(?:index|count)|step_count"):
        mark_pipeline_apply_succeeded(
            "run-1",
            trigger_id="trigger-1",
            repo_name="org/repo",
            pipeline="data/primary",
            step_index=step_index,
            step_count=step_count,
            pipeline_sha256="c" * 64,
        )


@patch("src.platform.aws.run_registry._shared._table")
def test_find_latest_successful_pipeline_apply_uses_indexed_query(mock_table):
    table = MagicMock()
    item = {
        "run_id": "run-1",
        "trigger_id": "trigger-1",
        "repo_name": "org/repo",
        "action": "apply",
        "status": "succeeded",
        "pipeline": "data/primary",
        "step_index": 1,
        "step_count": 3,
        "pipeline_sha256": "c" * 64,
        "expire_ttl": 9_999_999_999,
    }
    table.query.return_value = {"Items": [item]}
    mock_table.return_value = table

    result = find_latest_successful_pipeline_apply(
        trigger_id="trigger-1",
        repo_name="org/repo",
        pipeline="data/primary",
        step_index=1,
    )

    assert result == item
    query = table.query.call_args.kwargs
    assert query["IndexName"] == "pipeline_apply_step"
    assert query["ExpressionAttributeValues"] == {
        ":pk": pipeline_apply_gsi_pk(
            trigger_id="trigger-1",
            repo_name="org/repo",
            pipeline="data/primary",
            step_index=1,
        )
    }
    table.scan.assert_not_called()


@patch("src.platform.aws.dynamo_transactions.dynamo_client")
@patch("src.platform.aws.run_registry._shared._table")
def test_put_folder_attempt_replay_allows_equivalent_terminal_failure_status(mock_table, mock_client):
    table = MagicMock()
    table.name = "registry"
    client = MagicMock()
    client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}},
        "TransactWriteItems",
    )
    existing = {
        "pk": run_pk("run-1"),
        "sk": folder_attempt_sk("infra/a", 0),
        "run_id": "run-1",
        "folder": "infra/a",
        "account_id": "123456789012",
        "execution_id": "exec-1",
        "attempt": 0,
        "status": "failed",
        "manifest_sha256": "a" * 64,
        "updated_at": 1,
        "expire_ttl": expire_ttl(1),
        "outcome": {"succeeded": False, "error": "prepare failed"},
    }
    table.get_item.return_value = {"Item": existing}
    mock_table.return_value = table
    mock_client.return_value = client

    put_folder_attempt(
        run_id="run-1",
        folder="infra/a",
        account_id="123456789012",
        execution_id="exec-1",
        attempt=0,
        status="infrastructure_error",
        manifest_sha256="a" * 64,
        outcome={"succeeded": False, "error": "nested execution failed"},
    )

    table.get_item.assert_called_once()


@patch("src.platform.aws.dynamo_transactions.dynamo_client")
@patch("src.platform.aws.run_registry._shared._table")
def test_put_folder_attempt_replay_rejects_success_failure_status_mismatch(mock_table, mock_client):
    table = MagicMock()
    table.name = "registry"
    client = MagicMock()
    client.transact_write_items.side_effect = ClientError(
        {"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}},
        "TransactWriteItems",
    )
    existing = {
        "pk": run_pk("run-1"),
        "sk": folder_attempt_sk("infra/a", 0),
        "run_id": "run-1",
        "folder": "infra/a",
        "account_id": "123456789012",
        "execution_id": "exec-1",
        "attempt": 0,
        "status": "succeeded",
        "updated_at": 1,
        "expire_ttl": expire_ttl(1),
    }
    table.get_item.return_value = {"Item": existing}
    mock_table.return_value = table
    mock_client.return_value = client

    with pytest.raises(ValueError, match="attempt item replay mismatch on status"):
        put_folder_attempt(
            run_id="run-1",
            folder="infra/a",
            account_id="123456789012",
            execution_id="exec-1",
            attempt=0,
            status="failed",
        )
