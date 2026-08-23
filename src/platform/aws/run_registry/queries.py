# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run listing, pagination, and folder-gate queries for the run registry."""

from __future__ import annotations

import time
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-not-found]

from .keys import (
    folder_gate_pk,
    folder_gate_sk,
    pipeline_apply_gsi_pk,
    repo_gsi_pk,
    run_meta_sk,
    run_pk,
)

from . import _shared
from ._shared import (
    _FULL_SHA,
    _GATE_CURSOR,
    _MAX_GATE_OBSERVATIONS,
    _MAX_REPO_FILTER_BYTES,
    _MAX_RUN_LIST_EVALUATED_ITEMS,
    _MAX_RUN_LIST_EVALUATED_PAGES,
    _RUN_CURSOR,
    RunRegistryError,
    RunRegistryQueryError,
    _normalize,
    expire_ttl,
    is_expired,
)


def _validated_run_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not _RUN_CURSOR.fullmatch(cursor):
        raise RunRegistryQueryError("invalid run cursor")
    return cursor


def _validated_repo_filter(repo_filter: str | None) -> str | None:
    if repo_filter is None:
        return None
    value = repo_filter.strip()
    if not value:
        return None
    if len(value.encode("utf-8")) > _MAX_REPO_FILTER_BYTES or any(ord(char) < 32 for char in value):
        raise RunRegistryQueryError("invalid repo filter")
    return value.casefold()


def _run_sort_key(item: dict[str, Any]) -> str:
    value = item.get("gsi1sk")
    if not isinstance(value, str) or not _RUN_CURSOR.fullmatch(value):
        raise RunRegistryError("run registry row has invalid gsi1sk")
    return value


def _list_partition_matches(
    trigger_id: str,
    *,
    actions: frozenset[str],
    limit: int,
    before: str | None,
    repo_filter: str | None,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    matches: list[dict[str, Any]] = []
    exclusive_start_key: dict[str, Any] | None = None
    evaluated_pages = 0
    evaluated_items = 0
    while len(matches) < limit:
        remaining_items = _MAX_RUN_LIST_EVALUATED_ITEMS - evaluated_items
        values: dict[str, Any] = {":pk": repo_gsi_pk(trigger_id)}
        key_condition = "gsi1pk = :pk"
        if before is not None:
            values[":before"] = before
            key_condition += " AND gsi1sk < :before"
        query_kwargs: dict[str, Any] = {
            "IndexName": "repo_created",
            "KeyConditionExpression": key_condition,
            "ExpressionAttributeValues": values,
            "ScanIndexForward": False,
            "Limit": min(limit, remaining_items),
        }
        if exclusive_start_key is not None:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key
        response = _shared._table().query(**query_kwargs)
        raw_items = response.get("Items", [])
        evaluated_pages += 1
        evaluated_items += len(raw_items)
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise RunRegistryError("DynamoDB query returned a non-object run item")
            item = _normalize(raw_item)
            if item is None:
                raise RunRegistryError("DynamoDB query returned an empty run item")
            _run_sort_key(item)
            if is_expired(item):
                continue
            if str(item.get("action") or "").casefold() not in actions:
                continue
            repo_name = item.get("repo_name")
            if repo_filter is not None and (
                not isinstance(repo_name, str) or repo_filter not in repo_name.casefold()
            ):
                continue
            matches.append(item)
        last_key = response.get("LastEvaluatedKey")
        if len(matches) >= limit:
            return matches[:limit], len(matches) > limit or isinstance(last_key, dict), None
        if not isinstance(last_key, dict):
            return matches, False, None
        raw_boundary = last_key.get("gsi1sk")
        if not isinstance(raw_boundary, str) or not _RUN_CURSOR.fullmatch(raw_boundary):
            raise RunRegistryError("run registry query returned an invalid cursor")
        if (
            evaluated_pages >= _MAX_RUN_LIST_EVALUATED_PAGES
            or evaluated_items >= _MAX_RUN_LIST_EVALUATED_ITEMS
        ):
            return matches, True, raw_boundary
        exclusive_start_key = last_key
    return matches, False, None


def list_runs_authorized(
    trigger_ids: tuple[str, ...],
    *,
    actions: frozenset[str],
    repo_filter: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Merge authorized trigger partitions into one newest-first result space."""
    if not trigger_ids:
        raise RunRegistryQueryError("at least one authorized trigger_id is required")
    page_limit = min(max(1, limit), 100)
    before = _validated_run_cursor(cursor)
    normalized_repo = _validated_repo_filter(repo_filter)
    normalized_actions = frozenset(action.casefold() for action in actions)
    if not normalized_actions:
        raise RunRegistryQueryError("at least one authorized action is required")

    candidates: list[dict[str, Any]] = []
    partition_has_more = False
    evaluated_boundaries: list[str] = []
    for trigger_id in trigger_ids:
        items, has_more, evaluated_boundary = _list_partition_matches(
            trigger_id,
            actions=normalized_actions,
            limit=page_limit,
            before=before,
            repo_filter=normalized_repo,
        )
        candidates.extend(items)
        partition_has_more = partition_has_more or has_more
        if evaluated_boundary is not None:
            evaluated_boundaries.append(evaluated_boundary)
    candidates.sort(key=_run_sort_key, reverse=True)
    page = candidates[:page_limit]
    has_more = len(candidates) > page_limit or partition_has_more
    if page and len(page) >= page_limit and has_more:
        return page, _run_sort_key(page[-1])
    if evaluated_boundaries:
        boundary = max(evaluated_boundaries)
        safe_page = [item for item in page if _run_sort_key(item) >= boundary]
        return safe_page, boundary
    return page, None


def list_runs_for_repo(trigger_id: str, *, limit: int = 25, cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    before = _validated_run_cursor(cursor)

    query_kwargs: dict[str, Any] = {
        "IndexName": "repo_created",
        "KeyConditionExpression": "gsi1pk = :pk",
        "ExpressionAttributeValues": {":pk": repo_gsi_pk(trigger_id)},
        "ScanIndexForward": False,
        "Limit": min(max(1, limit), 100),
    }
    if before:
        query_kwargs["ExclusiveStartKey"] = {
            "gsi1pk": repo_gsi_pk(trigger_id),
            "gsi1sk": before,
            "pk": run_pk(before.split("#", 1)[-1]),
            "sk": run_meta_sk(),
        }
    response = _shared._table().query(**query_kwargs)
    items = [
        _normalize(item)
        for item in response.get("Items", [])
        if item and not is_expired(item)
    ]
    items = [item for item in items if item is not None]
    next_key = response.get("LastEvaluatedKey")
    next_cursor = None
    if next_key and isinstance(next_key.get("gsi1sk"), str):
        next_cursor = _validated_run_cursor(next_key["gsi1sk"])
    return items, next_cursor


def find_latest_successful_pipeline_apply(
    *,
    trigger_id: str,
    repo_name: str,
    pipeline: str,
    step_index: int,
) -> dict[str, Any] | None:
    """Return the newest successful apply run for one pipeline step."""
    gsi_pk = pipeline_apply_gsi_pk(
        trigger_id=trigger_id,
        repo_name=repo_name,
        pipeline=pipeline,
        step_index=step_index,
    )
    query_kwargs: dict[str, Any] = {
        "IndexName": "pipeline_apply_step",
        "KeyConditionExpression": "gsi2pk = :pk",
        "ExpressionAttributeValues": {":pk": gsi_pk},
        "ScanIndexForward": False,
    }
    while True:
        response = _shared._table().query(**query_kwargs)
        for raw_item in response.get("Items", []):
            if not isinstance(raw_item, dict):
                raise RunRegistryError("DynamoDB query returned a non-object pipeline apply item")
            item = _normalize(raw_item)
            if item is None:
                raise RunRegistryError("DynamoDB query returned an empty pipeline apply item")
            if is_expired(item):
                continue
            if item.get("status") != "succeeded" or item.get("action") != "apply":
                raise RunRegistryError("pipeline apply index contains a non-successful apply run")
            if item.get("pipeline") != pipeline or item.get("step_index") != step_index:
                raise RunRegistryError("pipeline apply index row identity does not match query")
            return item
        last_key = response.get("LastEvaluatedKey")
        if not isinstance(last_key, dict):
            return None
        query_kwargs["ExclusiveStartKey"] = last_key


def put_folder_gate_observations(
    *,
    run_id: str,
    trigger_id: str,
    repo_name: str,
    source_sha: str,
    folder_configs: dict[str, dict[str, Any]],
    observed_at: int | None = None,
) -> None:
    """Record the newest pinned-config apply/destroy flags observed by a run."""
    if not run_id or not trigger_id or not repo_name:
        raise ValueError("gate observation identity fields are required")
    if not _FULL_SHA.fullmatch(source_sha):
        raise ValueError("gate observation source_sha must be a full git SHA")
    if not folder_configs or len(folder_configs) > _MAX_GATE_OBSERVATIONS:
        raise ValueError("gate observations must contain between 1 and 50 folders")
    observed = int(time.time()) if observed_at is None else observed_at
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise ValueError("gate observation timestamp must be a non-negative integer")
    ttl = expire_ttl(observed)
    observation_key = f"{observed:020d}#{run_id}"
    table = _shared._table()
    for folder, config in folder_configs.items():
        if not isinstance(folder, str) or not folder:
            raise ValueError("gate observation folder must be a non-empty string")
        if not isinstance(config, dict):
            raise TypeError("gate observation folder config must be an object")
        apply = config.get("apply", False)
        destroy = config.get("destroy", False)
        if type(apply) is not bool or type(destroy) is not bool:
            raise ValueError("gate observation flags must be booleans")
        try:
            table.update_item(
                Key={"pk": folder_gate_pk(), "sk": folder_gate_sk(repo_name, folder)},
                UpdateExpression=(
                    "SET repo_name = :repo_name, folder = :folder, trigger_id = :trigger_id, "
                    "run_id = :run_id, source_sha = :source_sha, observed_at = :observed_at, "
                    "observed_sort_key = :observed_sort_key, expire_ttl = :ttl, "
                    "#apply = :apply, #destroy = :destroy"
                ),
                ConditionExpression=(
                    "attribute_not_exists(observed_sort_key) OR observed_sort_key <= :observed_sort_key"
                ),
                ExpressionAttributeNames={"#apply": "apply", "#destroy": "destroy"},
                ExpressionAttributeValues={
                    ":repo_name": repo_name,
                    ":folder": folder,
                    ":trigger_id": trigger_id,
                    ":run_id": run_id,
                    ":source_sha": source_sha.lower(),
                    ":observed_at": observed,
                    ":observed_sort_key": observation_key,
                    ":ttl": ttl,
                    ":apply": apply,
                    ":destroy": destroy,
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise


def _gate_projection(item: dict[str, Any]) -> dict[str, Any]:
    strings: dict[str, str] = {}
    for field in ("repo_name", "folder", "trigger_id", "run_id", "source_sha"):
        value = item.get(field)
        if not isinstance(value, str) or not value:
            raise RunRegistryError(f"folder gate row has invalid {field}")
        strings[field] = value
    if not _FULL_SHA.fullmatch(strings["source_sha"]):
        raise RunRegistryError("folder gate row has invalid source_sha")
    apply = item.get("apply")
    destroy = item.get("destroy")
    if type(apply) is not bool or type(destroy) is not bool:
        raise RunRegistryError("folder gate row has invalid opt-in flags")
    observed_at = item.get("observed_at")
    if isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0:
        raise RunRegistryError("folder gate row has invalid observed_at")
    return {
        **strings,
        "apply": apply,
        "destroy": destroy,
        "observed_at": observed_at,
    }


def list_folder_gate_projections(
    *,
    limit: int = 25,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    if cursor is not None and not _GATE_CURSOR.fullmatch(cursor):
        raise RunRegistryQueryError("invalid folder gate cursor")
    query: dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": folder_gate_pk()},
        "Limit": min(max(1, limit), 100),
    }
    if cursor:
        query["ExclusiveStartKey"] = {"pk": folder_gate_pk(), "sk": cursor}
    response = _shared._table().query(**query)
    folders: list[dict[str, Any]] = []
    for raw_item in response.get("Items", []):
        if not isinstance(raw_item, dict):
            raise RunRegistryError("DynamoDB query returned a non-object folder gate item")
        item = _normalize(raw_item)
        if item is None:
            raise RunRegistryError("DynamoDB query returned an empty folder gate item")
        if is_expired(item):
            continue
        folders.append(_gate_projection(item))
    last_key = response.get("LastEvaluatedKey")
    next_cursor = None
    if isinstance(last_key, dict):
        raw_cursor = last_key.get("sk")
        if not isinstance(raw_cursor, str) or not _GATE_CURSOR.fullmatch(raw_cursor):
            raise RunRegistryError("folder gate query returned an invalid cursor")
        next_cursor = raw_cursor
    return folders, next_cursor
