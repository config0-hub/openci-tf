"""Resolve safe folder runs and acquire their per-folder locks."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Protocol, cast

import boto3

from src.core.errors import BudgetUnmintableError, ConfigResolutionError, LockHeldError
from src.core.logging import get_logger
from src.core.models import FolderConfig
from src.domain.accounts.aliases import load_account_alias
from src.domain.accounts.binding import (
    account_binding_from_alias,
    account_binding_from_dict,
)
from src.domain.accounts.budget import compute_ttl, default_budget_for_action
from src.domain.deadlines import compute_deadline_at, deadline_epoch, format_deadline
from src.domain.command.affected_folders import (
    MAX_PR_CHANGED_FILES,
    resolve_affected_folders,
)
from src.domain.config.outer_state import discover_folders, resolve_outer_state
from src.domain.engine.artifact_limits import (
    MAX_GIT_URL_CHARS,
    MAX_REPO_NAME_CHARS,
    MAX_SSM_SETTING_PATH_CHARS,
    MAX_UPSTREAM_URL_CHARS,
)
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.inner_state import validate_inner_map_item
from src.domain.engine.invocation_id import derive_run_id
from src.domain.engine.outer_map_state import (
    build_compact_resolve_result,
    validate_folder_config_outer_size,
)
from src.domain.locks import run_lock
from src.domain.run.limits import MAX_FOLDERS_PER_REQUEST
from src.platform.aws.run_registry import set_run_deadline
from src.platform.aws.run_registry import set_run_pipeline_metadata
from src.platform.aws.run_registry import put_folder_gate_observations
from src.platform.aws.ssm import get_github_token
from src.platform.git.clone import cleanup_clone, shallow_clone
from src.platform.git.origin import validate_clone_source
from src.platform.git.package import validate_reserved_package_names
from src.platform.github.client import GitHubChangedFilesLimitExceeded, GitHubClient

logger = get_logger(__name__)

_SAFE_ACTIONS = frozenset({"plan", "drift", "report", "plan_destroy", "apply", "destroy"})
_MUTATION_ACTIONS = frozenset({"apply", "destroy"})
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_NO_OP_FINALIZATION_BUDGET_SECONDS = 900


class _PinnedPullRequestClient(Protocol):
    """Minimal GitHub PR operations needed to fetch a pinned changed-file list."""

    def get_pr_head_sha(self, repo: str, pr_number: int) -> str:
        """Return the current PR head SHA."""
        ...

    def get_pr_changed_files(
        self,
        repo: str,
        pr_number: int,
        *,
        max_files: int | None = None,
    ) -> list[dict]:
        """Return PR changed-file dictionaries, optionally bounded by count."""
        ...


def _pinned_commit_hash(webhook: dict[str, Any]) -> str:
    commit_hash = webhook.get("commit_hash")
    if not isinstance(commit_hash, str) or not _FULL_SHA.fullmatch(commit_hash):
        raise ValueError("commit_hash must be a full 40-character git SHA")
    return commit_hash


def _assert_pr_head_matches(
    client: _PinnedPullRequestClient,
    repo: str,
    pr_number: int,
    pinned_sha: str,
    *,
    position: str,
) -> None:
    current_sha = client.get_pr_head_sha(repo, pr_number)
    if current_sha.lower() != pinned_sha.lower():
        raise ConfigResolutionError(
            f"pull request head SHA changed {position} changed-file retrieval; "
            f"pinned {pinned_sha}, current {current_sha}"
        )


def _changed_files_for_pinned_pr(
    client: _PinnedPullRequestClient,
    repo: str,
    pr_number: int,
    pinned_sha: str,
) -> list[dict]:
    _assert_pr_head_matches(client, repo, pr_number, pinned_sha, position="before")
    try:
        changed_files = client.get_pr_changed_files(
            repo,
            pr_number,
            max_files=MAX_PR_CHANGED_FILES,
        )
    except GitHubChangedFilesLimitExceeded as error:
        raise ConfigResolutionError(str(error)) from error
    _assert_pr_head_matches(client, repo, pr_number, pinned_sha, position="after")
    return changed_files


def _selected_folders(
    event: dict[str, Any],
    clone_dir: str,
    token: str,
    commit_hash: str,
) -> list[str]:
    webhook = event["webhook_info"]
    if event.get("affected_flag"):
        pr_number = webhook.get("pr_number")
        if not isinstance(pr_number, int):
            raise ValueError("pr_number is required to resolve affected folders")
        configured = discover_folders(Path(clone_dir))
        client = GitHubClient(token)
        changed_files = _changed_files_for_pinned_pr(
            client,
            webhook["repo_name"],
            pr_number,
            commit_hash,
        )
        return resolve_affected_folders(changed_files, configured)
    if event.get("all_flag"):
        return discover_folders(Path(clone_dir))
    requested = event.get("folders", [])
    if not isinstance(requested, list) or not all(isinstance(folder, str) for folder in requested):
        raise ValueError("folders must be a list of strings")
    return requested



def _bounded_setting_path(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigResolutionError(f"{field} must be a string")
    if len(value) > MAX_SSM_SETTING_PATH_CHARS:
        raise ConfigResolutionError(f"{field} exceeds {MAX_SSM_SETTING_PATH_CHARS} characters")
    return value


def _bounded_repo_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigResolutionError("repo_name must be a non-empty string")
    if len(value) > MAX_REPO_NAME_CHARS:
        raise ConfigResolutionError(f"repo_name exceeds {MAX_REPO_NAME_CHARS} characters")
    return value


def _bounded_git_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigResolutionError("git_url must be a non-empty string")
    if len(value) > MAX_GIT_URL_CHARS:
        raise ConfigResolutionError(f"git_url exceeds {MAX_GIT_URL_CHARS} characters")
    return value


def _bounded_upstream_urls(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        raise ConfigResolutionError("upstream_urls must be an object prepared by configuration resolution")
    bounded: dict[str, str] = {}
    for key, url in value.items():
        if not isinstance(key, str) or not key:
            raise ConfigResolutionError("upstream_urls keys must be non-empty strings")
        if len(url) > MAX_UPSTREAM_URL_CHARS:
            raise ConfigResolutionError(f"upstream_urls entry for {key} exceeds {MAX_UPSTREAM_URL_CHARS} characters")
        bounded[key] = url
    return bounded


def _folder_step_indexes(steps: list[Any]) -> dict[str, int]:
    indexes: dict[str, int] = {}
    for step_index, step in enumerate(steps):
        if not isinstance(step, list) or not step:
            raise ConfigResolutionError("resolved steps must contain non-empty folder lists")
        for folder in step:
            if not isinstance(folder, str) or not folder:
                raise ConfigResolutionError("resolved steps must contain non-empty folder strings")
            if folder in indexes:
                raise ConfigResolutionError(f"folder {folder!r} appears in multiple steps")
            indexes[folder] = step_index
    return indexes


def _confirmed_pipeline_state_step_index(webhook: dict[str, Any]) -> int | None:
    pipeline = webhook.get("pipeline")
    step_index = webhook.get("pipeline_step_index")
    step_count = webhook.get("pipeline_step_count")
    if pipeline is None and step_index is None and step_count is None:
        return None
    if not isinstance(pipeline, str) or not pipeline:
        raise ConfigResolutionError("pipeline must be a non-empty string")
    if type(step_index) is not int or step_index < 1:
        raise ConfigResolutionError("pipeline_step_index must be an integer >= 1")
    if type(step_count) is not int or step_count < 1 or step_index > step_count:
        raise ConfigResolutionError("pipeline_step_count must be an integer >= pipeline_step_index")
    return step_index - 1


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Return bounded Map input and skipped lock outcomes for a safe action."""
    action = event["action"]
    if action not in _SAFE_ACTIONS:
        raise ValueError(f"unsafe action: {action}")
    settings, webhook = event["settings"], event["webhook_info"]
    repo = webhook["repo_name"]
    logger.info("validate_and_resolve handler invoked", extra={"repo": repo, "action": action})
    token = get_github_token(settings["ssm_openci_tf_github_token"])
    commit_hash = _pinned_commit_hash(webhook)
    validated_url = validate_clone_source(settings["git_url"], webhook.get("repo_name", ""))
    clone_dir = shallow_clone(validated_url, repo_name=repo, commit_hash=commit_hash, token=token)
    try:
        validate_reserved_package_names(clone_dir)
        raw_pipeline = event.get("pipeline")
        if raw_pipeline is not None and (not isinstance(raw_pipeline, str) or not raw_pipeline):
            raise ConfigResolutionError("pipeline must be a non-empty string")
        pipeline = raw_pipeline if isinstance(raw_pipeline, str) else None
        if pipeline is None:
            folders = _selected_folders(event, clone_dir, token, commit_hash)
            if not folders:
                if event.get("affected_flag"):
                    return {
                        **event,
                        "run_id": event.get("run_id") or derive_run_id(webhook),
                        "deadline_at": format_deadline(
                            int(time.time()) + _NO_OP_FINALIZATION_BUDGET_SECONDS
                        ),
                        "folders": [],
                        "steps": [],
                        "map_items": [],
                        "current_step_items": [],
                        "step_index": 0,
                        "step_count": 0,
                        "outcomes": [],
                        "skipped": [],
                        "no_op_reason": "no configured Terraform folders are affected by this pull request",
                    }
                raise ConfigResolutionError("no configured folders found")
        else:
            folders = []
        if pipeline is None:
            resolved = resolve_outer_state(
                clone_dir,
                folders,
                settings["upstream_urls"],
                action,
            )
        else:
            resolved = resolve_outer_state(
                clone_dir,
                folders,
                settings["upstream_urls"],
                action,
                pipeline=pipeline,
            )
    finally:
        cleanup_clone(clone_dir)
    configs = resolved["folder_configs"]
    resolved_folders = resolved.get("folders", folders)
    if not isinstance(resolved_folders, list):
        raise ConfigResolutionError("resolved folders must be a list")
    folders = [folder for folder in resolved_folders if folder in configs]
    resolved_steps = resolved.get("steps", [folders])
    if not isinstance(resolved_steps, list):
        raise ConfigResolutionError("resolved steps must be a list")
    steps = resolved_steps
    folder_step_indexes = _folder_step_indexes(steps)
    confirmed_pipeline_step_index = None
    if action in _MUTATION_ACTIONS and event.get("intent_confirmed") is True:
        confirmed_pipeline_step_index = _confirmed_pipeline_state_step_index(webhook)
    if confirmed_pipeline_step_index is not None:
        folder_step_indexes = {folder: confirmed_pipeline_step_index for folder in folders}
    if len(folders) > MAX_FOLDERS_PER_REQUEST:
        raise ConfigResolutionError(f"resolved folders exceed maximum of {MAX_FOLDERS_PER_REQUEST}")
    if not folders:
        raise ValueError("no folders resolved")
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        run_id = derive_run_id(webhook)
    upstream_urls = _bounded_upstream_urls(resolved["upstream_urls"])
    repo_name = _bounded_repo_name(webhook["repo_name"])
    git_url = _bounded_git_url(settings["git_url"])
    github_token_path = _bounded_setting_path(settings["ssm_openci_tf_github_token"], field="ssm_openci_tf_github_token")
    infracost_key_path = _bounded_setting_path(settings.get("ssm_infracost_api_key", ""), field="ssm_infracost_api_key")
    table = cast(Any, boto3.resource("dynamodb")).Table(os.environ["LOCKS_TABLE_NAME"])
    now = int(time.time())
    candidates: list[dict[str, Any]] = []
    folder_windows: list[tuple[int, int]] = []
    for folder in folders:
        raw = configs.get(folder)
        if not isinstance(raw, dict):
            raise TypeError(f"missing folder config for {folder}")
        config = FolderConfig(**raw)
        pin: dict[str, Any] | None = None
        if action in _MUTATION_ACTIONS:
            folder_pins = event.get("folder_pins")
            if not isinstance(folder_pins, dict):
                raise ConfigResolutionError("mutation requires folder_pins")
            raw_pin = folder_pins.get(folder)
            if not isinstance(raw_pin, dict):
                raise ConfigResolutionError(
                    f"mutation requires pinned plan for folder {folder!r}"
                )
            pin = raw_pin
            binding = account_binding_from_dict(pin.get("account_binding"))
            if pin.get("account_id") != binding.account_id:
                raise ConfigResolutionError(
                    f"intent account binding mismatch for folder {folder!r}"
                )
        else:
            binding = account_binding_from_alias(
                load_account_alias(config.account_alias)
            )
        account_id = binding.account_id
        budget = default_budget_for_action(action, config.timeout)
        try:
            compute_ttl(budget, binding.max_ttl)
        except BudgetUnmintableError as error:
            raise ConfigResolutionError(
                f"folder {folder!r} timeout exceeds mintable target credential lifetime"
            ) from error
        attempt = 0
        execution_id = compose_execution_id(run_id, folder, attempt)
        item = {
            "run_id": run_id,
            "folder": folder,
            "account_id": account_id,
            "account_binding": binding.to_compact(),
            "action": action,
            "attempt": attempt,
            "budget": budget,
            "step_index": folder_step_indexes[folder],
            "folder_config": raw,
            "upstream_urls": upstream_urls,
            "execution_id": execution_id,
            "repo_name": repo_name,
            "git_url": git_url,
            "commit_hash": commit_hash,
            "ssm_openci_tf_github_token": github_token_path,
            "ssm_infracost_api_key": infracost_key_path,
        }
        grace_seconds = 0
        if action in _MUTATION_ACTIONS:
            source_plan_run_id = event.get("source_plan_run_id")
            if not isinstance(source_plan_run_id, str) or not source_plan_run_id:
                raise ConfigResolutionError("mutation requires source_plan_run_id")
            if pin is None:
                raise ConfigResolutionError(
                    f"mutation requires pinned plan for folder {folder!r}"
                )
            item["folder_pin"] = pin
            item["source_plan_run_id"] = source_plan_run_id
            grace_seconds = config.resolved_grace_seconds(action)
            item["grace_seconds"] = grace_seconds
        folder_windows.append((budget, grace_seconds))
        validate_folder_config_outer_size(raw)
        candidates.append(item)
    deadline_at = compute_deadline_at(
        action, folder_windows, resolved_at=now
    )
    for item in candidates:
        item["deadline_at"] = deadline_at
        validate_inner_map_item(item)
    resolved_event = {
        **event,
        "folders": folders,
        "steps": steps,
        "deadline_at": deadline_at,
        "no_op_reason": None,
    }
    build_compact_resolve_result(
        resolved_event, run_id=run_id, full_items=candidates, skipped=[]
    )
    if os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        set_run_deadline(run_id, deadline_at)
        if pipeline is not None:
            set_run_pipeline_metadata(run_id, pipeline=pipeline, step_count=len(steps))
        trigger_id = webhook.get("trigger_id")
        if not isinstance(trigger_id, str) or not trigger_id:
            raise ConfigResolutionError("trigger_id is required for folder gate observations")
        put_folder_gate_observations(
            run_id=run_id,
            trigger_id=trigger_id,
            repo_name=repo_name,
            source_sha=commit_hash,
            folder_configs={folder: configs[folder] for folder in folders},
            observed_at=now,
        )
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    acquired: list[tuple[str, str]] = []
    completed = False
    try:
        for item in candidates:
            folder = item["folder"]
            execution_id = item["execution_id"]
            try:
                run_lock.acquire(
                    table,
                    repo,
                    folder,
                    execution_id,
                    now,
                    deadline_epoch(deadline_at) - now,
                    run_id,
                    deadline_at,
                )
            except LockHeldError as error:
                if pipeline is not None:
                    raise ConfigResolutionError(
                        f"folder {folder!r} is locked during pipeline resolution"
                    ) from error
                if action in _MUTATION_ACTIONS:
                    raise ConfigResolutionError(
                        f"folder {folder!r} is locked during ordered mutation"
                    ) from error
                skipped.append({
                    "folder": folder,
                    "account_id": item["account_id"],
                    "status": "in_progress",
                    "reply": run_lock.in_progress_reply(str(error).removeprefix("run already in progress (exec ").removesuffix(")")),
                })
                continue
            acquired.append((folder, execution_id))
            items.append(item)
        result = build_compact_resolve_result(
            resolved_event, run_id=run_id, full_items=items, skipped=skipped
        )
        completed = True
        logger.info("validate_and_resolve handler completed", extra={"run_id": run_id, "repo": repo, "action": action})
        return result
    finally:
        if not completed and acquired:
            run_lock.release_all(table, run_id)
