# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply/destroy intent gate validation."""
from __future__ import annotations

import time
from typing import Protocol

from src.core.errors import ConfigValidationError
from src.core.models import FolderConfig, RepoSettings
from src.domain.config.pipeline import Pipeline
from src.domain.accounts.aliases import AccountAlias, load_account_alias
from src.domain.accounts.binding import account_binding_from_alias
from src.domain.intent.models import (
    FolderPlanPin,
    IntentGateFailure,
    IntentGateResult,
    IntentRecord,
)
from src.domain.intent.plan_lookup import find_newest_fresh_plan_run
from src.domain.intent.token import mint_intent_id, mint_token


class _ApprovalClient(Protocol):
    def pr_has_approved_review(self, repo: str, pr_number: int) -> bool:
        ...


def folders_for_pipeline_mutation_gate(
    pipeline: Pipeline,
    checkpoint_index: int,
    *,
    reverse: bool = False,
) -> list[str]:
    """Return folders checked by the preliminary gate before creating a pipeline intent."""
    if checkpoint_index < 1:
        raise ValueError("checkpoint_index must be an integer >= 1")
    all_folders: list[str] = []
    for step in pipeline.steps:
        all_folders.extend(step.folders)
    if reverse:
        all_folders = list(reversed(all_folders))
    if checkpoint_index == 1:
        return all_folders
    return all_folders[checkpoint_index - 1 :]


def folders_for_pipeline_apply_gate(pipeline: Pipeline, pipeline_step: int) -> list[str]:
    """Backward-compatible alias for preliminary pipeline gate folder selection."""
    return folders_for_pipeline_mutation_gate(pipeline, pipeline_step, reverse=False)


def _folder_config_allows(action: str, config: FolderConfig) -> bool:
    if action == "apply":
        return config.apply.allow is True
    if action == "destroy":
        return config.destroy.allow is True
    raise ValueError(f"unsupported intent action: {action}")


def _plan_after_prior_checkpoint(
    *,
    plan_created_at: int,
    prior_checkpoint_completed_at: int | None,
) -> bool:
    if prior_checkpoint_completed_at is None:
        return True
    if type(prior_checkpoint_completed_at) is not int or prior_checkpoint_completed_at < 0:
        return False
    return plan_created_at > prior_checkpoint_completed_at


def evaluate_intent_gates(
    *,
    action: str,
    folders: list[str],
    folder_configs: dict[str, FolderConfig],
    settings: RepoSettings,
    pr_number: int,
    commit_hash: str,
    approval_client: _ApprovalClient | None = None,
    now: int | None = None,
    prior_checkpoint_completed_at: int | None = None,
    source_plan_run_id: str | None = None,
) -> IntentGateResult:
    """Run the ask-if tree for apply/destroy step 1."""
    failures: list[IntentGateFailure] = []
    account_by_folder: dict[str, AccountAlias] = {}

    for folder in folders:
        config = folder_configs.get(folder)
        if config is None:
            failures.append(IntentGateFailure(f"folder {folder!r} has no .openci_tf/config.yaml", folder=folder))
            continue
        try:
            account = load_account_alias(config.account_alias)
        except ConfigValidationError as exc:
            failures.append(
                IntentGateFailure(
                    f"account alias {config.account_alias!r} is invalid or not registered: {exc}",
                    folder=folder,
                )
            )
            continue
        account_by_folder[folder] = account
        if not account.enable_apply:
            failures.append(
                IntentGateFailure(
                    f"apply/destroy not enabled for account {config.account_alias} ({account.account_id})",
                    folder=folder,
                )
            )
        if not _folder_config_allows(action, config):
            failures.append(
                IntentGateFailure(
                    f"folder {folder!r} does not declare {action}.allow: true in .openci_tf/config.yaml",
                    folder=folder,
                )
            )

    if settings.require_approval:
        if approval_client is None:
            failures.append(IntentGateFailure("approval check unavailable"))
        elif not approval_client.pr_has_approved_review(settings.repo_name, pr_number):
            failures.append(IntentGateFailure("pull request lacks an approved review"))

    if failures:
        return IntentGateResult(ok=False, failures=failures)

    pins: list[FolderPlanPin] = []
    source_run_ids: set[str] = set()
    for folder in folders:
        config = folder_configs[folder]
        account_id = account_by_folder[folder].account_id
        lookup = find_newest_fresh_plan_run(
            trigger_id=settings.trigger_id,
            repo_name=settings.repo_name,
            pr_number=pr_number,
            folder=folder,
            mutation_action=action,
            commit_hash=commit_hash,
            account_id=account_id,
            expected_tf_runtime=config.tf_runtime,
        )
        if lookup.match is None:
            if lookup.stale:
                if action == "apply":
                    message = f"stale plan — re-run tf plan {folder}"
                else:
                    message = f"stale plan — re-run tf plan --destroy {folder}"
            else:
                message = "no fresh plan — run tf plan first"
            failures.append(IntentGateFailure(message, folder=folder))
            continue
        match = lookup.match
        plan_run_id = str(match["run_id"])
        plan_created_at = match.get("created_at")
        if type(plan_created_at) is not int or plan_created_at <= 0:
            failures.append(
                IntentGateFailure(
                    "plan record is missing a valid creation timestamp",
                    folder=folder,
                )
            )
            continue
        if source_plan_run_id is not None and plan_run_id != source_plan_run_id:
            failures.append(
                IntentGateFailure(
                    "plan does not match the pinned checkpoint plan",
                    folder=folder,
                )
            )
            continue
        if not _plan_after_prior_checkpoint(
            plan_created_at=plan_created_at,
            prior_checkpoint_completed_at=prior_checkpoint_completed_at,
        ):
            failures.append(
                IntentGateFailure(
                    "plan predates prior pipeline checkpoint — create a fresh plan for this folder",
                    folder=folder,
                )
            )
            continue
        source_run_ids.add(plan_run_id)
        pins.append(
            FolderPlanPin(
                folder=folder,
                source_run_id=plan_run_id,
                plan_sha256=str(match["plan_sha256"]),
                plan_artifact_name=str(match["plan_artifact_name"]),
                account_id=account_id,
                tf_runtime=str(match["tf_runtime"]),
                account_binding=account_binding_from_alias(
                    account_by_folder[folder]
                ).to_dict(),
            )
        )

    if failures:
        return IntentGateResult(ok=False, failures=failures)

    if len(folders) > 1 and len(source_run_ids) != 1:
        failures.append(IntentGateFailure("folders resolve to different source plan runs"))
        return IntentGateResult(ok=False, failures=failures)

    source_run_id = next(iter(source_run_ids)) if source_run_ids else ""
    current = int(time.time()) if now is None else now
    token = mint_token()
    record = IntentRecord(
        token=token,
        intent_id=mint_intent_id(),
        trigger_id=settings.trigger_id,
        pr_number=pr_number,
        action=action,
        source_run_id=source_run_id,
        folders=tuple(folders),
        commit_hash=commit_hash.lower(),
        folder_pins=tuple(pins),
        expires_at=current + 600,
        used=False,
    )
    return IntentGateResult(ok=True, record=record)


def evaluate_confirm_gates(
    *,
    record: IntentRecord,
    commit_hash: str,
    trigger_id: str,
    pr_number: int,
    repo_name: str,
    now: int | None = None,
) -> list[IntentGateFailure]:
    """Validate a confirm step against a loaded intent record."""
    failures: list[IntentGateFailure] = []
    current = int(time.time()) if now is None else now
    if record.used:
        failures.append(IntentGateFailure("confirmation token already used"))
    if current >= record.expires_at:
        failures.append(IntentGateFailure("confirmation token expired"))
    if record.trigger_id != trigger_id:
        failures.append(IntentGateFailure("confirmation token does not match this repository trigger"))
    if record.pr_number != pr_number:
        failures.append(IntentGateFailure("confirmation token does not match this pull request"))
    if commit_hash.lower() != record.commit_hash.lower():
        failures.append(IntentGateFailure("PR moved since plan — re-plan"))
    return failures
