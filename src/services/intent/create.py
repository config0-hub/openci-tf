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
from src.domain.config.pipeline import Pipeline, canonical_pipeline_sha256, load_pipeline
from src.domain.intent.gates import evaluate_intent_gates, folders_for_pipeline_apply_gate
from src.domain.intent.models import IntentGateFailure
from src.platform.aws.dynamo import get_repo_settings
from src.platform.aws.ssm import get_github_token
from src.platform.git.clone import cleanup_clone, shallow_clone
from src.platform.git.origin import validate_clone_source
from src.platform.github.client import GitHubClient
from src.platform.aws.run_registry import RunRegistryError, find_latest_successful_pipeline_apply
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


def _create_pipeline_apply_intent(
    *,
    action: str,
    settings: RepoSettings,
    approval_token: str,
    pipeline_name: str,
    pipeline_step: int,
    pr_number: int,
    commit_hash: str,
    requested_comment_id: int | None = None,
    requested_comment_body: str | None = None,
) -> tuple[IntentGateFailure | None, dict[str, Any] | None]:
    if pipeline_step < 1:
        return IntentGateFailure("pipeline step must be an integer >= 1"), None
    pipeline, folder_configs, pipeline_hash = _pipeline_for_intent(
        settings=settings,
        commit_hash=commit_hash,
        pipeline_name=pipeline_name,
    )
    step_count = len(pipeline.steps)
    if pipeline_step > step_count:
        return (
            IntentGateFailure(
                f"pipeline {pipeline.name} step {pipeline_step} is out of range; step_count={step_count}"
            ),
            None,
        )
    if pipeline_step > 1:
        prior_step = pipeline_step - 1
        try:
            prior = find_latest_successful_pipeline_apply(
                trigger_id=settings.trigger_id,
                repo_name=settings.repo_name,
                pipeline=pipeline.name,
                step_index=prior_step,
            )
        except RunRegistryError as error:
            raise IntentCreationError(str(error)) from error
        if prior is None:
            return (
                IntentGateFailure(
                    f"pipeline {pipeline.name} step {pipeline_step} requires a completed apply of step {prior_step} first"
                ),
                None,
            )
        if prior.get("pipeline_sha256") != pipeline_hash:
            return (
                IntentGateFailure(
                    f"pipeline {pipeline.name} changed since step {prior_step} was applied; restart from step 1"
                ),
                None,
            )
    gate_folders = folders_for_pipeline_apply_gate(pipeline, pipeline_step)
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
    step_folders = list(pipeline.steps[pipeline_step - 1].folders)
    step_gates = evaluate_intent_gates(
        action=action,
        folders=step_folders,
        folder_configs=folder_configs,
        settings=settings,
        pr_number=pr_number,
        commit_hash=commit_hash,
        approval_client=_GitHubApprovalClient(approval_token),
    )
    if not step_gates.ok or step_gates.record is None:
        return step_gates.failures[0] if step_gates.failures else IntentGateFailure("intent gate failed"), None
    record = replace(
        step_gates.record,
        pipeline=pipeline.name,
        step_index=pipeline_step,
        step_count=step_count,
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
) -> tuple[IntentGateFailure | None, dict[str, Any] | None]:
    settings = get_repo_settings(trigger_id, with_webhook_secret=False)
    token = get_github_token(settings.ssm_openci_tf_github_token)
    if pipeline is not None:
        if action != "apply":
            return IntentGateFailure("destroy pipeline is not supported"), None
        return _create_pipeline_apply_intent(
            action=action,
            settings=settings,
            approval_token=token,
            pipeline_name=pipeline,
            pipeline_step=1 if pipeline_step is None else pipeline_step,
            pr_number=pr_number,
            commit_hash=commit_hash,
            requested_comment_id=requested_comment_id,
            requested_comment_body=requested_comment_body,
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
