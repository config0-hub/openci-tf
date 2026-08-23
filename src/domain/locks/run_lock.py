"""Durable run-scoped ownership for per-folder DynamoDB locks."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from src.core.errors import LockHeldError
from src.domain.deadlines import deadline_epoch
from src.platform.aws.dynamo_transactions import transact_write_items

# Keep both the lock and its ownership index recoverable after the permitted
# execution window. DynamoDB TTL deletion is asynchronous; this margin also
# gives EventBridge and Lambda retries a deterministic closure window.
LOCK_CLOSER_MARGIN_SECONDS = 3600


def _lock_key(repo: str, folder: str) -> dict[str, str]:
    return {"pk": "lock", "sk": f"{repo}/{folder}"}


def _ownership_key(run_id: str, repo: str, folder: str) -> dict[str, str]:
    return {"pk": f"run-locks#{run_id}", "sk": f"lock#{repo}/{folder}"}


def _is_condition_failure(error: ClientError) -> bool:
    return error.response["Error"]["Code"] in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }


def acquire(
    table: Any,
    repo: str,
    folder: str,
    execution_id: str,
    now: int,
    ttl: int,
    run_id: str | None = None,
    deadline_at: str | None = None,
) -> None:
    """Acquire a folder lock and, when supplied, its durable run ownership row.

    The six-argument form remains available for low-level callers and fixtures.
    Production resolution supplies ``run_id`` and ``deadline_at`` so the two rows
    are committed atomically and the lease cannot expire before the run deadline.
    """
    if ttl <= 0:
        raise ValueError("lock ttl must be positive")
    if (run_id is None) != (deadline_at is None):
        raise ValueError("run_id and deadline_at must be supplied together")
    lock_key = _lock_key(repo, folder)
    if run_id is None or deadline_at is None:
        try:
            table.put_item(
                Item={
                    **lock_key,
                    "holder_execution_id": execution_id,
                    "expires_at": now + ttl,
                },
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
                ExpressionAttributeValues={":now": now},
            )
        except ClientError as error:
            if not _is_condition_failure(error):
                raise
            holder = (
                table.get_item(Key=lock_key)
                .get("Item", {})
                .get("holder_execution_id", "unknown")
            )
            raise LockHeldError(
                f"run already in progress (exec {holder})"
            ) from error
        return

    permitted_until = deadline_epoch(deadline_at)
    if permitted_until <= now:
        raise ValueError("cannot acquire a lock for an expired deadline")
    expires_at = max(now + ttl, permitted_until) + LOCK_CLOSER_MARGIN_SECONDS
    ownership_key = _ownership_key(run_id, repo, folder)
    table_name = table.name
    try:
        transact_write_items(
            table.meta.client,
            transact_items=[
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            **lock_key,
                            "holder_execution_id": execution_id,
                            "holder_run_id": run_id,
                            "deadline_at": deadline_at,
                            "expires_at": expires_at,
                        },
                        "ConditionExpression": "attribute_not_exists(pk) OR expires_at < :now",
                        "ExpressionAttributeValues": {":now": now},
                    }
                },
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            **ownership_key,
                            "run_id": run_id,
                            "repo": repo,
                            "folder": folder,
                            "execution_id": execution_id,
                            "deadline_at": deadline_at,
                            "expires_at": expires_at,
                        },
                    }
                },
            ],
        )
    except ClientError as error:
        if not _is_condition_failure(error):
            raise
        holder = (
            table.get_item(Key=lock_key, ConsistentRead=True)
            .get("Item", {})
            .get("holder_execution_id", "unknown")
        )
        raise LockHeldError(f"run already in progress (exec {holder})") from error


def release(table: Any, repo: str, folder: str, execution_id: str) -> None:
    """Release only the named holder; late and duplicate releases are idempotent."""
    lock_key = _lock_key(repo, folder)
    current = table.get_item(Key=lock_key, ConsistentRead=True).get("Item", {})
    holder_run_id = current.get("holder_run_id")
    try:
        table.delete_item(
            Key=lock_key,
            ConditionExpression="holder_execution_id = :holder",
            ExpressionAttributeValues={":holder": execution_id},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise
    if isinstance(holder_run_id, str) and holder_run_id:
        table.delete_item(Key=_ownership_key(holder_run_id, repo, folder))


def ownerships_for_run(table: Any, run_id: str) -> list[dict[str, Any]]:
    """Load every durable lock identity for a run, including paginated rows."""
    query: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": f"run-locks#{run_id}"},
        "ConsistentRead": True,
    }
    rows: list[dict[str, Any]] = []
    while True:
        response = table.query(**query)
        rows.extend(item for item in response.get("Items", []) if isinstance(item, dict))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return rows
        query["ExclusiveStartKey"] = last_key


def release_all(table: Any, run_id: str) -> int:
    """Release all locks recovered from the durable run index.

    A holder-checked lock delete happens before its ownership row is removed. If
    an AWS call fails, the row remains and a normal/EventBridge retry can resume.
    A late closer cannot delete a lock since acquired by another execution.
    """
    released = 0
    for ownership in ownerships_for_run(table, run_id):
        repo = ownership.get("repo")
        folder = ownership.get("folder")
        execution_id = ownership.get("execution_id")
        if not all(isinstance(value, str) and value for value in (repo, folder, execution_id)):
            raise ValueError("corrupt durable lock ownership row")
        release(table, repo, folder, execution_id)
        table.delete_item(Key={"pk": ownership["pk"], "sk": ownership["sk"]})
        released += 1
    return released


def in_progress_reply(holder_execution_id: str) -> str:
    """Build the user-facing duplicate-run reply."""
    return f"Run already in progress (exec {holder_execution_id})."
