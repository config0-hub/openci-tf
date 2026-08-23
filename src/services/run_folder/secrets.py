# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Secret assembly for run-folder engine submissions."""

from __future__ import annotations

import os

from src.core.models import FolderConfig
from src.domain.engine.artifact_paths import build_folder_artifact_keys_for_run
from src.domain.engine.run_artifact_layout import resolve_run_artifact_layout
from src.domain.engine.plan_artifacts import plan_retention_days
from src.platform.aws import s3
from src.platform.aws.ssm import get_infracost_api_key


def _plan_artifact_secrets(
    *,
    action: str,
    bucket: str,
    repo_name: str,
    run_id: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    expiry: int,
    config: FolderConfig,
) -> tuple[dict[str, str], str]:
    if action not in {"plan", "report"}:
        return {}, ""
    layout = resolve_run_artifact_layout(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        action=action,
    )
    keys = layout.folder_keys
    plan_uri = f"s3://{bucket}/{keys.plan_tfplan}"
    metadata_uri = f"s3://{bucket}/{keys.plan_metadata}"
    return {
        "PLAN_BINARY_PUT_URL": s3.presign_put(bucket, keys.plan_tfplan, expiry),
        "PLAN_SHA256_PUT_URL": s3.presign_put(bucket, keys.plan_sha256, expiry),
        "PLAN_METADATA_PUT_URL": s3.presign_put(bucket, keys.plan_metadata, expiry),
        "OPENCI_TF_PLAN_S3_URI": plan_uri,
        "OPENCI_TF_PLAN_SHA256_S3_URI": f"s3://{bucket}/{keys.plan_sha256}",
        "OPENCI_TF_PLAN_METADATA_S3_URI": metadata_uri,
        "OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS": str(plan_retention_days()),
        "OPENCI_TF_REPO_NAME": repo_name,
        "OPENCI_TF_RUN_ID": run_id,
        "OPENCI_TF_PINNED_SHA": commit_hash.lower(),
        "OPENCI_TF_ACCOUNT_ID": account_id,
        "OPENCI_TF_FOLDER": folder,
        "OPENCI_TF_ACTION": action,
        "OPENCI_TF_TF_RUNTIME": f"{config.binary}:{config.runtime_version}",
    }, metadata_uri


def _destroy_plan_artifact_secrets(
    *,
    action: str,
    bucket: str,
    repo_name: str,
    run_id: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    expiry: int,
    config: FolderConfig,
) -> tuple[dict[str, str], str]:
    if action != "plan_destroy":
        return {}, ""
    layout = resolve_run_artifact_layout(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        action=action,
    )
    keys = layout.folder_keys
    plan_uri = f"s3://{bucket}/{keys.destroy_plan_tfplan}"
    metadata_uri = f"s3://{bucket}/{keys.destroy_plan_metadata}"
    return {
        "DESTROY_PLAN_BINARY_PUT_URL": s3.presign_put(bucket, keys.destroy_plan_tfplan, expiry),
        "DESTROY_PLAN_SHA256_PUT_URL": s3.presign_put(bucket, keys.destroy_plan_sha256, expiry),
        "DESTROY_PLAN_METADATA_PUT_URL": s3.presign_put(bucket, keys.destroy_plan_metadata, expiry),
        "OPENCI_TF_DESTROY_PLAN_S3_URI": plan_uri,
        "OPENCI_TF_DESTROY_PLAN_SHA256_S3_URI": f"s3://{bucket}/{keys.destroy_plan_sha256}",
        "OPENCI_TF_DESTROY_PLAN_METADATA_S3_URI": metadata_uri,
        "OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS": str(plan_retention_days()),
        "OPENCI_TF_REPO_NAME": repo_name,
        "OPENCI_TF_RUN_ID": run_id,
        "OPENCI_TF_PINNED_SHA": commit_hash.lower(),
        "OPENCI_TF_ACCOUNT_ID": account_id,
        "OPENCI_TF_FOLDER": folder,
        "OPENCI_TF_ACTION": action,
        "OPENCI_TF_TF_RUNTIME": f"{config.binary}:{config.runtime_version}",
    }, metadata_uri


def _pr_number_for_source_run(source_run_id: str) -> int | None:
    if not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return None
    from botocore.exceptions import BotoCoreError, ClientError

    from src.domain.run.pr_context import pr_number_from_run_record
    from src.platform.aws.run_registry import RunRegistryError, get_run

    try:
        return pr_number_from_run_record(get_run(source_run_id))
    except (RunRegistryError, ClientError, BotoCoreError, OSError) as exc:
        raise RunRegistryError(
            f"run registry lookup failed for {source_run_id}: {exc}"
        ) from exc
    except ValueError as exc:
        raise RunRegistryError(
            f"invalid run record for {source_run_id}: {exc}"
        ) from exc


def _pinned_plan_secrets(
    *,
    action: str,
    bucket: str,
    repo_name: str,
    source_run_id: str,
    folder: str,
    plan_sha256: str,
    plan_artifact_name: str,
    expiry: int,
) -> dict[str, str]:
    if action not in {"apply", "destroy"}:
        return {}
    if action == "apply" and plan_artifact_name != "plan.tfplan":
        raise ValueError("apply may only consume plan.tfplan")
    if action == "destroy" and plan_artifact_name != "destroy.plan.tfplan":
        raise ValueError("destroy may only consume destroy.plan.tfplan")
    pr_number = _pr_number_for_source_run(source_run_id)
    pointer_type = None
    if pr_number is not None:
        pointer_type = (
            "destroy" if plan_artifact_name == "destroy.plan.tfplan" else "plan"
        )
    keys = build_folder_artifact_keys_for_run(
        repo_name=repo_name,
        run_id=source_run_id,
        folder_path=folder,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    key = keys.plan_tfplan if plan_artifact_name == "plan.tfplan" else keys.destroy_plan_tfplan
    return {
        "PINNED_PLAN_GET_URL": s3.presign_get(bucket, key, expiry),
        "OPENCI_TF_PINNED_PLAN_SHA256": plan_sha256,
        "OPENCI_TF_PLAN_ARTIFACT_NAME": plan_artifact_name,
        "OPENCI_TF_SOURCE_PLAN_RUN_ID": source_run_id,
    }


def _infracost_secret(
    action: str, ssm_path: object, existing: dict[str, str]
) -> dict[str, str]:
    if (
        action not in {"plan", "report"}
        or not isinstance(ssm_path, str)
        or not ssm_path.strip()
    ):
        return {}
    if "INFRACOST_API_KEY" in existing:
        raise ValueError(
            "INFRACOST_API_KEY already configured; dedicated Infracost secret required"
        )
    return {"INFRACOST_API_KEY": get_infracost_api_key(ssm_path)}
