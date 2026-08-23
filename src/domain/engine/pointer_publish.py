# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PR-scoped execution pointer publish with ordered epoch clobber."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from botocore.exceptions import ClientError  # type: ignore[import-not-found]

from src.domain.engine.artifact_paths import parse_execution_pointer, serialize_execution_pointer
from src.domain.engine.outer_execution_id import parse_outer_run_epoch, validate_outer_run_id


class PointerConflictError(RuntimeError):
    """Raised when a conditional pointer write loses a race."""


@dataclass(frozen=True)
class PointerPublishResult:
    key: str
    execution_id: str
    updated: bool
    skipped_stale: bool


def _execution_id_rank(execution_id: str) -> tuple[int, str]:
    validated = validate_outer_run_id(execution_id)
    epoch = parse_outer_run_epoch(validated)
    if epoch is None:
        return (0, validated)
    return (epoch, validated)


def _should_clobber(current_execution_id: str, proposed_execution_id: str) -> bool:
    current_rank = _execution_id_rank(current_execution_id)
    proposed_rank = _execution_id_rank(proposed_execution_id)
    if proposed_rank[0] > current_rank[0]:
        return True
    if proposed_rank[0] < current_rank[0]:
        return False
    if proposed_rank[1] == current_rank[1]:
        return False
    return proposed_rank[1] > current_rank[1]


def publish_execution_pointer(
    *,
    bucket: str,
    key: str,
    execution_id: str,
    head_object: Callable[[str, str], dict | None],
    put_text: Callable[..., None],
    get_text: Callable[[str, str], bytes | None],
) -> PointerPublishResult:
    """Publish one PR-scoped pointer with ordered epoch clobber semantics."""
    validate_outer_run_id(execution_id)
    existing_head = head_object(bucket, key)
    if existing_head is None:
        put_text(bucket=bucket, key=key, body=serialize_execution_pointer(execution_id))
        return PointerPublishResult(
            key=key, execution_id=execution_id, updated=True, skipped_stale=False
        )

    current_body = get_text(bucket, key)
    if current_body is None:
        raise ValueError(f"pointer head exists without readable body: s3://{bucket}/{key}")
    current_execution_id = parse_execution_pointer(current_body.decode("utf-8"))
    if current_execution_id == execution_id:
        return PointerPublishResult(
            key=key, execution_id=execution_id, updated=False, skipped_stale=False
        )
    if not _should_clobber(current_execution_id, execution_id):
        return PointerPublishResult(
            key=key,
            execution_id=current_execution_id,
            updated=False,
            skipped_stale=True,
        )

    etag = existing_head.get("etag")
    if not isinstance(etag, str) or not etag:
        raise ValueError(f"pointer missing ETag for conditional write: s3://{bucket}/{key}")
    try:
        put_text(
            bucket=bucket,
            key=key,
            body=serialize_execution_pointer(execution_id),
            if_match=etag,
        )
    except ClientError as error:
        if error.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
            raise PointerConflictError(
                f"pointer changed during publish: s3://{bucket}/{key}"
            ) from error
        raise
    return PointerPublishResult(
        key=key, execution_id=execution_id, updated=True, skipped_stale=False
    )
