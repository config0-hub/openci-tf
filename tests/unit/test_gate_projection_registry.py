# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pinned repository-config gate projection adapter tests."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from src.platform.aws import run_registry
from src.platform.aws.run_registry.keys import (
    folder_gate_pk,
    folder_gate_sk,
)

_FULL_SHA = "a" * 40


def test_put_folder_gate_observations_records_sha_bound_flags(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    run_registry.put_folder_gate_observations(
        run_id="run-1",
        trigger_id="trigger-1",
        repo_name="org/repo",
        source_sha=_FULL_SHA.upper(),
        folder_configs={
            "infra/a": {"apply": True, "destroy": False},
            "infra/b": {"apply": False, "destroy": True},
        },
        observed_at=1_700_000_000,
    )

    assert table.update_item.call_count == 2
    first = table.update_item.call_args_list[0].kwargs
    assert first["Key"] == {
        "pk": folder_gate_pk(),
        "sk": folder_gate_sk("org/repo", "infra/a"),
    }
    assert first["ExpressionAttributeValues"] == {
        ":repo_name": "org/repo",
        ":folder": "infra/a",
        ":trigger_id": "trigger-1",
        ":run_id": "run-1",
        ":source_sha": _FULL_SHA,
        ":observed_at": 1_700_000_000,
        ":observed_sort_key": "00000000001700000000#run-1",
        ":ttl": run_registry.expire_ttl(1_700_000_000),
        ":apply": True,
        ":destroy": False,
    }
    assert "observed_sort_key <= :observed_sort_key" in first["ConditionExpression"]


def test_stale_gate_observation_loses_without_overwriting_newer_projection(monkeypatch):
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "newer row exists"}},
        "UpdateItem",
    )
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    run_registry.put_folder_gate_observations(
        run_id="older-run",
        trigger_id="trigger-1",
        repo_name="org/repo",
        source_sha=_FULL_SHA,
        folder_configs={"infra/a": {"apply": False, "destroy": False}},
        observed_at=1_700_000_000,
    )


def test_gate_observation_propagates_unexpected_write_error(monkeypatch):
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "outage"}},
        "UpdateItem",
    )
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    with pytest.raises(ClientError):
        run_registry.put_folder_gate_observations(
            run_id="run-1",
            trigger_id="trigger-1",
            repo_name="org/repo",
            source_sha=_FULL_SHA,
            folder_configs={"infra/a": {"apply": True, "destroy": False}},
        )


def test_list_folder_gate_projections_returns_allowlisted_latest_observations(monkeypatch):
    sentinel = "SENTINEL-SECRET"
    cursor = folder_gate_sk("org/repo", "infra/b")
    table = MagicMock()
    table.query.return_value = {
        "Items": [
            {
                "pk": folder_gate_pk(),
                "sk": folder_gate_sk("org/repo", "infra/a"),
                "repo_name": "org/repo",
                "folder": "infra/a",
                "trigger_id": "trigger-1",
                "run_id": "run-1",
                "source_sha": _FULL_SHA,
                "apply": True,
                "destroy": False,
                "observed_at": Decimal(1_700_000_000),
                "expire_ttl": Decimal(4_102_444_800),
                "observed_sort_key": "00000000001700000000#run-1",
                "secret": sentinel,
            }
        ],
        "LastEvaluatedKey": {"pk": folder_gate_pk(), "sk": cursor},
    }
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    folders, next_cursor = run_registry.list_folder_gate_projections(limit=500)

    assert folders == [
        {
            "repo_name": "org/repo",
            "folder": "infra/a",
            "trigger_id": "trigger-1",
            "run_id": "run-1",
            "source_sha": _FULL_SHA,
            "apply": True,
            "destroy": False,
            "observed_at": 1_700_000_000,
        }
    ]
    assert sentinel not in repr(folders)
    assert next_cursor == cursor
    assert table.query.call_args.kwargs["Limit"] == 100


def test_list_folder_gate_projections_rejects_forged_cursor_before_io(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(run_registry._shared, "_table", lambda: table)

    with pytest.raises(run_registry.RunRegistryQueryError, match="invalid folder gate cursor"):
        run_registry.list_folder_gate_projections(cursor="forged")

    table.query.assert_not_called()
