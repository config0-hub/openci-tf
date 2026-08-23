"""Folder attempt transaction adapter tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.platform.aws.dynamo_transactions import transact_write_items
from src.platform.aws.run_registry.keys import (
    folder_attempt_sk,
    folder_summary_sk,
    run_pk,
)
from src.platform.aws.run_registry import get_folder_attempt, put_folder_attempt


@patch("src.platform.aws.run_registry._shared.dynamo_client")
@patch("src.platform.aws.run_registry._shared._table")
def test_put_folder_attempt_serializes_update_key(mock_table, mock_client):
    table = MagicMock()
    table.name = "registry"
    client = MagicMock()
    mock_table.return_value = table
    mock_client.return_value = client

    put_folder_attempt(
        run_id="run-1",
        folder="infra/a",
        account_id="123456789012",
        execution_id="exec-1",
        attempt=0,
        status="failed",
        manifest_s3_uri="s3://tmp/exec-1/manifest.json",
        manifest_sha256="a" * 64,
        outcome={"succeeded": False, "error": "boom"},
        drift_detected=True,
        step_index=3,
    )

    client.transact_write_items.assert_called_once()
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    update = items[1]["Update"]
    expressions = update["UpdateExpression"] + " " + update["ConditionExpression"]
    for value_name in update["ExpressionAttributeValues"]:
        assert value_name in expressions
    assert ":exec_match" not in update["ExpressionAttributeValues"]
    assert update["ExpressionAttributeValues"][":drift_detected"]["BOOL"] is True
    assert update["ExpressionAttributeValues"][":step_index"]["N"] == "3"
    assert items[0]["Put"]["Item"]["drift_detected"]["BOOL"] is True
    assert items[0]["Put"]["Item"]["step_index"]["N"] == "3"
    key = update["Key"]
    assert key["pk"]["S"] == run_pk("run-1")
    assert key["sk"]["S"] == folder_summary_sk("infra/a")
    assert "Item" not in items[0]["Put"] or isinstance(
        items[0]["Put"]["Item"]["pk"], dict
    )


def test_put_folder_attempt_rejects_out_of_bounds_step_index():
    with pytest.raises(ValueError, match="step_index"):
        put_folder_attempt(
            run_id="run-1",
            folder="infra/a",
            account_id="123456789012",
            execution_id="exec-1",
            attempt=0,
            status="failed",
            step_index=21,
        )


@patch("src.platform.aws.run_registry._shared._table")
def test_get_folder_attempt_reads_exact_attempt_consistently(mock_table):
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "pk": run_pk("run-1"),
            "sk": folder_attempt_sk("infra/a", 2),
            "execution_id": "exec-2",
            "expire_ttl": 9999999999,
        }
    }
    mock_table.return_value = table

    result = get_folder_attempt("run-1", "infra/a", 2)

    assert result is not None and result["execution_id"] == "exec-2"
    table.get_item.assert_called_once_with(
        Key={"pk": run_pk("run-1"), "sk": folder_attempt_sk("infra/a", 2)},
        ConsistentRead=True,
    )


def test_transact_write_items_serializes_key_directly():
    client = MagicMock()
    transact_write_items(
        client,
        transact_items=[
            {
                "Update": {
                    "TableName": "registry",
                    "Key": {"pk": "run#1", "sk": "folder#infra"},
                    "UpdateExpression": "SET #status = :status",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {":status": "failed"},
                }
            }
        ],
    )
    encoded = client.transact_write_items.call_args.kwargs["TransactItems"][0][
        "Update"
    ]["Key"]
    assert encoded["pk"]["S"] == "run#1"
    assert encoded["sk"]["S"] == "folder#infra"
