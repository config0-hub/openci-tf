# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create apply/destroy intent tokens from webhook or API ingress."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from src.core.errors import ConfigResolutionError, ConfigValidationError
from src.core.models import FolderConfig, RepoSettings
from src.domain.config.outer_state import discover_folders, resolve_outer_state
from src.domain.config.pipeline import (
    Pipeline,
    canonical_pipeline_sha256,
    checkpoint_count,
    folder_at_checkpoint,
    load_pipeline,
)
from src.domain.intent.gates import evaluate_intent_gates, folders_for_pipeline_mutation_gate
from src.domain.intent.models import IntentGateFailure
from src.platform.aws.dynamo import get_repo_settings
from src.platform.aws.ssm import get_github_token
from src.platform.git.clone import cleanup_clone, shallow_clone
from src.platform.git.origin import validate_clone_source
from src.platform.github.client import GitHubClient
from src.platform.aws.run_registry import (
    RunRegistryError,
    find_latest_successful_pipeline_checkpoint,
)
from src.services.intent.registry import IntentRegistryError, put_intent


class IntentCreationError(RuntimeError):
    pass


class _GitHubApprovalClient:
    def __init__(self, token: str) -> None:
        self._client = GitHubClient(token)

    def pr_has_approved_review(self, repo: str, pr_number: int) -> bool:
        return self._client.pr_has_approved_review(repo, pr_number)


def _folder_configs_for_intent(
    *,
    settings: RepoSettings,
    commit_hash: str,
    folders: list[str],
) -> dict[str, FolderConfig]:
    token = get_github_token(settings.ssm_openci_tf_github_token)
    validated_url = validate_clone_source(settings.git_url, settings.repo_name)
    clone_dir = shallow_clone(validated_url, repo_name=settings.repo_name, commit_hash=commit_hash, token=token)
    try:
        return _folder_configs_from_clone(clone_dir, settings=settings, folders=folders)
    finally:
        cleanup_clone(clone_dir)


def _folder_configs_from_clone(
    clone_dir: str,
    *,
    settings: RepoSettings,
    folders: list[str],
) -> dict[str, FolderConfig]:
    configured = discover_folders(Path(clone_dir))
    missing = [folder for folder in folders if folder not in configured]
    if missing:
        raise IntentCreationError(f"unknown folder: {', '.join(missing)}")
    resolved = resolve_outer_state(clone_dir, folders, settings.upstream_urls, "plan")
    raw_configs = resolved["folder_configs"]
    return {folder: FolderConfig(**raw) for folder, raw in raw_configs.items()}


def _pipeline_for_intent(
    *,
    settings: RepoSettings,
    commit_hash: str,
    pipeline_name: str,
) -> tuple[Pipeline, dict[str, FolderConfig], str]:
    token = get_github_token(settings.ssm_openci_tf_github_token)
    validated_url = validate_clone_source(settings.git_url, settings.repo_name)
    clone_dir = shallow_clone(validated_url, repo_name=settings.repo_name, commit_hash=commit_hash, token=token)
    try:
        try:
            pipeline = load_pipeline(Path(clone_dir), pipeline_name)
        except (ConfigResolutionError, ConfigValidationError) as error:
            raise IntentCreationError(str(error)) from error
        folders = [folder for step in pipeline.steps for folder in step.folders]
        folder_configs = _folder_configs_from_clone(
            clone_dir,
            settings=settings,
            folders=folders,
        )
        return pipeline, folder_configs, canonical_pipeline_sha256(pipeline)
    finally:
        cleanup_clone(clone_dir)


def _create_pipeline_mutation_intent(
    *,
    action: str,
    settings: RepoSettings,
    approval_token: str,
    pipeline_name: str,
    checkpoint_index: int,
    pr_number: int,
    commit_hash: str,
    requested_comment_id: int | None = None,
    requested_comment_body: str | None = None,
    source_plan_run_id: str | None = None,
) -> tuple[IntentGateFailure | None, dict[str, Any] | None]:
    if action not in {"apply", "destroy"}:
        return IntentGateFailure(f"unsupported pipeline mutation action: {action}"), None
    if checkpoint_index < 1:
        return IntentGateFailure("pipeline step must be an integer >= 1"), None
    reverse = action == "destroy"
    pipeline, folder_configs, pipeline_hash = _pipeline_for_intent(
        settings=settings,
        commit_hash=commit_hash,
        pipeline_name=pipeline_name,
    )
    total_checkpoints = checkpoint_count(pipeline)
    if checkpoint_index > total_checkpoints:
        return (
            IntentGateFailure(
                f"pipeline {pipeline.name} step {checkpoint_index} is out of range; step_count={total_checkpoints}"
            ),
            None,
        )
    prior_checkpoint_completed_at: int | None = None
    if checkpoint_index > 1:
        prior_checkpoint = checkpoint_index - 1
        try:
            prior = find_latest_successful_pipeline_checkpoint(
                trigger_id=settings.trigger_id,
                repo_name=settings.repo_name,
                pipeline=pipeline.name,
                action=action,
                step_index=prior_checkpoint,
                pr_number=pr_number,
                commit_hash=commit_hash,
                pipeline_sha256=pipeline_hash,
            )
        except RunRegistryError as error:
            raise IntentCreationError(str(error)) from error
        if prior is None:
            return (
                IntentGateFailure(
                    f"pipeline {pipeline.name} step {checkpoint_index} requires a completed "
                    f"{action} of step {prior_checkpoint} first"
                ),
                None,
            )
        if prior.get("pipeline_sha256") != pipeline_hash:
            return (
                IntentGateFailure(
                    f"pipeline {pipeline.name} changed since step {prior_checkpoint} was "
                    f"{'applied' if action == 'apply' else 'destroyed'}; restart from step 1"
                ),
                None,
            )
        prior_run_id = prior.get("run_id")
        if not isinstance(prior_run_id, str) or not prior_run_id:
            return (
                IntentGateFailure(
                    f"pipeline {pipeline.name} step {checkpoint_index} requires a completed "
                    f"{action} of step {prior_checkpoint} first"
                ),
                None,
            )
        completed_at = prior.get("pipeline_checkpoint_completed_at")
        if type(completed_at) is not int or completed_at < 0:
            return (
                IntentGateFailure(
                    f"pipeline {pipeline.name} step {checkpoint_index} requires a completed "
                    f"{action} of step {prior_checkpoint} first"
                ),
                None,
            )
        prior_checkpoint_completed_at = completed_at
    gate_folders = folders_for_pipeline_mutation_gate(
        pipeline, checkpoint_index, reverse=reverse
    )
    all_gates = evaluate_intent_gates(
        action=action,
        folders=gate_folders,
        folder_configs=folder_configs,
        settings=settings,
        pr_number=pr_number,
        commit_hash=commit_hash,
        approval_client=_GitHubApprovalClient(approval_token),
    )
    if not all_gates.ok:
        return all_gates.failures[0] if all_gates.failures else IntentGateFailure("intent gate failed"), None
    checkpoint_folder = folder_at_checkpoint(
        pipeline, checkpoint_index, reverse=reverse
    )
    checkpoint_gates = evaluate_intent_gates(
        action=action,
        folders=[checkpoint_folder],
        folder_configs=folder_configs,
        settings=settings,
        pr_number=pr_number,
        commit_hash=commit_hash,
        approval_client=_GitHubApprovalClient(approval_token),
        prior_checkpoint_completed_at=prior_checkpoint_completed_at,
        source_plan_run_id=source_plan_run_id,
    )
    if not checkpoint_gates.ok or checkpoint_gates.record is None:
        return (
            checkpoint_gates.failures[0]
            if checkpoint_gates.failures
            else IntentGateFailure("intent gate failed")
        ), None
    record = replace(
        checkpoint_gates.record,
        pipeline=pipeline.name,
        step_index=checkpoint_index,
        step_count=total_checkpoints,
        pipeline_sha256=pipeline_hash,
        requested_comment_id=requested_comment_id,
        requested_comment_body=requested_comment_body,
    )
    try:
        put_intent(record)
    except IntentRegistryError as error:
        raise IntentCreationError(str(error)) from error
    return None, record.to_dict()


def create_intent(
    *,
    action: str,
    folders: list[str],
    trigger_id: str,
    pr_number: int,
    commit_hash: str,
    pipeline: str | None = None,
    pipeline_step: int | None = None,
    requested_comment_id: int | None = None,
    requested_comment_body: str | None = None,
    source_plan_run_id: str | None = None,
) -> tuple[IntentGateFailure | None, dict[str, Any] | None]:
    settings = get_repo_settings(trigger_id, with_webhook_secret=False)
    token = get_github_token(settings.ssm_openci_tf_github_token)
    if pipeline is not None:
        return _create_pipeline_mutation_intent(
            action=action,
            settings=settings,
            approval_token=token,
            pipeline_name=pipeline,
            checkpoint_index=1 if pipeline_step is None else pipeline_step,
            pr_number=pr_number,
            commit_hash=commit_hash,
            requested_comment_id=requested_comment_id,
            requested_comment_body=requested_comment_body,
            source_plan_run_id=source_plan_run_id,
        )
    folder_configs = _folder_configs_for_intent(settings=settings, commit_hash=commit_hash, folders=folders)
    result = evaluate_intent_gates(
        action=action,
        folders=folders,
        folder_configs=folder_configs,
        settings=settings,
        pr_number=pr_number,
        commit_hash=commit_hash,
        approval_client=_GitHubApprovalClient(token),
    )
    if not result.ok or result.record is None:
        return result.failures[0] if result.failures else IntentGateFailure("intent gate failed"), None
    record = replace(
        result.record,
        requested_comment_id=requested_comment_id,
        requested_comment_body=requested_comment_body,
    )
    try:
        put_intent(record)
    except IntentRegistryError as error:
        raise IntentCreationError(str(error)) from error
    return None, record.to_dict()
