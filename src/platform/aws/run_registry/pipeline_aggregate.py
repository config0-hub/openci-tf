# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persist stable pipeline aggregate comment identity and checkpoint history."""

from __future__ import annotations

import time
from typing import Any

from . import _shared
from .keys import pipeline_aggregate_pk
from ._shared import RunRegistryError, _normalize

# Must match domain.run.limits.MAX_FOLDERS_PER_REQUEST (platform layer cannot import domain).
_MAX_CHECKPOINT_ROWS = 50


def _aggregate_sk() -> str:
    return "aggregate"


def get_pipeline_aggregate_state(
    *,
    trigger_id: str,
    repo_name: str,
    pipeline: str,
    action: str,
    pr_number: int,
    commit_hash: str,
    pipeline_sha256: str,
) -> dict[str, Any] | None:
    pk = pipeline_aggregate_pk(
        trigger_id=trigger_id,
        repo_name=repo_name,
        pipeline=pipeline,
        action=action,
        pr_number=pr_number,
        commit_hash=commit_hash,
        pipeline_sha256=pipeline_sha256,
    )
    item = _normalize(
        _shared._table()
        .get_item(Key={"pk": pk, "sk": _aggregate_sk()}, ConsistentRead=True)
        .get("Item")
    )
    return item


def save_pipeline_aggregate_state(
    *,
    trigger_id: str,
    repo_name: str,
    pipeline: str,
    action: str,
    pr_number: int,
    commit_hash: str,
    pipeline_sha256: str,
    comment_id: int,
    checkpoint_rows: list[dict[str, Any]],
) -> None:
    if type(comment_id) is not int or comment_id < 1:
        raise ValueError("comment_id must be a positive integer")
    if not isinstance(checkpoint_rows, list):
        raise ValueError("checkpoint_rows must be a list")
    bounded_rows = checkpoint_rows[-_MAX_CHECKPOINT_ROWS:]
    cumulative_succeeded = sum(
        1 for row in checkpoint_rows if row.get("succeeded") is True
    )
    cumulative_failed = sum(
        1 for row in checkpoint_rows if row.get("succeeded") is False
    )
    pk = pipeline_aggregate_pk(
        trigger_id=trigger_id,
        repo_name=repo_name,
        pipeline=pipeline,
        action=action,
        pr_number=pr_number,
        commit_hash=commit_hash,
        pipeline_sha256=pipeline_sha256,
    )
    now = int(time.time())
    try:
        _shared._table().put_item(
            Item={
                "pk": pk,
                "sk": _aggregate_sk(),
                "comment_id": comment_id,
                "checkpoint_rows": bounded_rows,
                "cumulative_succeeded": cumulative_succeeded,
                "cumulative_failed": cumulative_failed,
                "updated_at": now,
            }
        )
    except Exception as error:
        raise RunRegistryError("failed to persist pipeline aggregate state") from error
