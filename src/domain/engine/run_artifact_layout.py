"""Resolve artifact key layout for one outer run folder execution."""

from __future__ import annotations

import os
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-not-found]

from src.domain.engine.artifact_paths import (
    FolderArtifactKeys,
    build_folder_artifact_keys,
    build_folder_artifact_keys_for_run,
    manifest_key,
    pointer_type_for_action,
)
from src.domain.run.pr_context import pr_number_from_run_record
from src.platform.aws.run_registry import RunRegistryError, get_run


@dataclass(frozen=True)
class RunArtifactLayout:
    folder_keys: FolderArtifactKeys
    pr_number: int | None
    pointer_type: str | None


def resolve_run_artifact_layout(
    *,
    repo_name: str,
    run_id: str,
    folder_path: str,
    action: str,
) -> RunArtifactLayout:
    pr_number = None
    if os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        try:
            pr_number = pr_number_from_run_record(get_run(run_id))
        except (RunRegistryError, ClientError, BotoCoreError, OSError) as exc:
            raise RunRegistryError(
                f"run registry lookup failed for {run_id}: {exc}"
            ) from exc
        except ValueError as exc:
            raise RunRegistryError(
                f"invalid run record for {run_id}: {exc}"
            ) from exc
    pointer_type = None
    if pr_number is not None:
        pointer_type = pointer_type_for_action(action)
        folder_keys = build_folder_artifact_keys_for_run(
            repo_name=repo_name,
            run_id=run_id,
            folder_path=folder_path,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
    else:
        folder_keys = build_folder_artifact_keys(
            repo_name=repo_name,
            run_id=run_id,
            folder_path=folder_path,
        )
    return RunArtifactLayout(
        folder_keys=folder_keys,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )


def manifest_key_for_layout(
    layout: RunArtifactLayout,
    *,
    repo_name: str,
    run_id: str,
    folder_path: str,
) -> str:
    return manifest_key(
        repo_name,
        run_id,
        folder_path,
        pr_number=layout.pr_number,
        pointer_type=layout.pointer_type,
    )
