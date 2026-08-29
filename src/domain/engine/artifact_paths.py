# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure S3 artifact key builders for the openci-tf/<repo>/<run_id>/<folder>/ layout.

Binary plan uploads use plain presigned PUT (not create-only If-None-Match) so
retries within one run can overwrite partial objects; manifest sha256 verification
remains the integrity gate for committed outcomes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.domain.engine.invocation_id import assert_execution_id_bounds

OPENCI_TF_PREFIX = "openci-tf"
MANIFEST_KEY_SUFFIX = "manifest.json"
_LATEST_ALIAS = "latest"
_RUN_ID = re.compile(r"^[\w+=,.@-]+$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_S3_URI = re.compile(r"^s3://([^/]+)/(.+)$")


@dataclass(frozen=True)
class FolderArtifactKeys:
    prefix: str
    init_out: str
    validate_out: str
    plan_out: str
    drift_json: str
    tfsec_json: str
    tfsec_output: str
    infracost_json: str
    infracost_output: str
    manifest_json: str
    plan_tfplan: str
    plan_sha256: str
    plan_metadata: str
    destroy_plan_out: str
    destroy_plan_tfplan: str
    destroy_plan_sha256: str
    destroy_plan_metadata: str


@dataclass(frozen=True)
class PlanArtifactKeys:
    plan: str
    checksum: str
    metadata: str


@dataclass(frozen=True)
class DestroyPlanArtifactKeys:
    plan: str
    checksum: str
    metadata: str
    plan_out: str


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    text = run_id.strip()
    assert_execution_id_bounds(text)
    if text == _LATEST_ALIAS:
        raise ValueError(
            "run_id must not be the literal 'latest' (collides with latest/ alias)"
        )
    if not _RUN_ID.fullmatch(text):
        raise ValueError("run_id contains disallowed characters")
    return text


def _validate_literal_segment(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    if value.startswith("/"):
        raise ValueError(f"{label} must not start with /")
    if "\\" in value:
        raise ValueError(f"{label} must not contain backslashes")
    if _CONTROL_CHARS.search(value):
        raise ValueError(f"{label} must not contain control characters")
    parts = value.split("/")
    if any(not part.strip() for part in parts):
        raise ValueError(f"{label} must not contain empty path segments")
    if ".." in parts:
        raise ValueError(f"{label} must not contain .. segments")
    if parts[0] == _LATEST_ALIAS:
        raise ValueError(
            f"{label} must not use 'latest' as the first path segment (collides with latest/ alias)"
        )
    return value


def folder_artifact_prefix(*, repo_name: str, run_id: str, folder_path: str) -> str:
    """Return the run-scoped folder prefix ending with /."""
    repo = _validate_literal_segment(repo_name, label="repo_name")
    run = validate_run_id(run_id)
    folder = _validate_literal_segment(folder_path, label="folder_path")
    return f"{OPENCI_TF_PREFIX}/{repo}/{run}/{folder}/"


def latest_folder_prefix(*, repo_name: str, folder_path: str) -> str:
    """Return the latest pointer prefix ending with /."""
    repo = _validate_literal_segment(repo_name, label="repo_name")
    folder = _validate_literal_segment(folder_path, label="folder_path")
    return f"{OPENCI_TF_PREFIX}/{repo}/latest/{folder}/"


def artifact_key(
    *, repo_name: str, run_id: str, folder_path: str, relative_name: str
) -> str:
    if not isinstance(relative_name, str) or not relative_name.strip():
        raise ValueError("relative_name is required")
    if relative_name.startswith("/") or "\\" in relative_name:
        raise ValueError("relative_name must be a relative object name")
    if ".." in relative_name.split("/"):
        raise ValueError("relative_name must not contain .. segments")
    return f"{folder_artifact_prefix(repo_name=repo_name, run_id=run_id, folder_path=folder_path)}{relative_name}"


def manifest_key(
    repo_name: str,
    run_id: str,
    folder: str,
    *,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> str:
    if pr_number is not None and pointer_type is not None:
        prefix = execution_artifact_prefix(
            repo_name=repo_name,
            pr_number=pr_number,
            execution_id=run_id,
            pointer_type=pointer_type,
            folder_path=folder,
        )
        return f"{prefix}{MANIFEST_KEY_SUFFIX}"
    return artifact_key(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder,
        relative_name=MANIFEST_KEY_SUFFIX,
    )


def build_folder_artifact_keys(
    *, repo_name: str, run_id: str, folder_path: str
) -> FolderArtifactKeys:
    prefix = folder_artifact_prefix(
        repo_name=repo_name, run_id=run_id, folder_path=folder_path
    )
    return FolderArtifactKeys(
        prefix=prefix,
        init_out=f"{prefix}init.out",
        validate_out=f"{prefix}validate.out",
        plan_out=f"{prefix}tf/plan.out",
        drift_json=f"{prefix}drift.json",
        tfsec_json=f"{prefix}tfsec.json",
        tfsec_output=f"{prefix}tfsec.output",
        infracost_json=f"{prefix}infracost.json",
        infracost_output=f"{prefix}infracost.output",
        manifest_json=f"{prefix}manifest.json",
        plan_tfplan=f"{prefix}tf/plan.tfplan",
        plan_sha256=f"{prefix}tf/plan.tfplan.sha256",
        plan_metadata=f"{prefix}tf/plan-metadata.json",
        destroy_plan_out=f"{prefix}destroy.plan.out",
        destroy_plan_tfplan=f"{prefix}tf/destroy.plan.tfplan",
        destroy_plan_sha256=f"{prefix}tf/destroy.plan.tfplan.sha256",
        destroy_plan_metadata=f"{prefix}tf/destroy-plan-metadata.json",
    )


def build_plan_artifact_keys(
    *, repo_name: str, run_id: str, folder_path: str
) -> PlanArtifactKeys:
    keys = build_folder_artifact_keys(
        repo_name=repo_name, run_id=run_id, folder_path=folder_path
    )
    return PlanArtifactKeys(
        plan=keys.plan_tfplan, checksum=keys.plan_sha256, metadata=keys.plan_metadata
    )


def build_destroy_plan_artifact_keys(
    *, repo_name: str, run_id: str, folder_path: str
) -> DestroyPlanArtifactKeys:
    keys = build_folder_artifact_keys(
        repo_name=repo_name, run_id=run_id, folder_path=folder_path
    )
    return DestroyPlanArtifactKeys(
        plan=keys.destroy_plan_tfplan,
        checksum=keys.destroy_plan_sha256,
        metadata=keys.destroy_plan_metadata,
        plan_out=keys.destroy_plan_out,
    )


def expected_destroy_plan_artifact_uris(
    *,
    bucket: str,
    repo_name: str,
    run_id: str,
    folder_path: str,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> DestroyPlanArtifactKeys:
    if pr_number is not None and pointer_type is not None:
        folder_keys = build_folder_artifact_keys_for_run(
            repo_name=repo_name,
            run_id=run_id,
            folder_path=folder_path,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        return DestroyPlanArtifactKeys(
            plan=f"s3://{bucket}/{folder_keys.destroy_plan_tfplan}",
            checksum=f"s3://{bucket}/{folder_keys.destroy_plan_sha256}",
            metadata=f"s3://{bucket}/{folder_keys.destroy_plan_metadata}",
            plan_out=f"s3://{bucket}/{folder_keys.destroy_plan_out}",
        )
    keys = build_destroy_plan_artifact_keys(
        repo_name=repo_name, run_id=run_id, folder_path=folder_path
    )
    return DestroyPlanArtifactKeys(
        plan=f"s3://{bucket}/{keys.plan}",
        checksum=f"s3://{bucket}/{keys.checksum}",
        metadata=f"s3://{bucket}/{keys.metadata}",
        plan_out=f"s3://{bucket}/{keys.plan_out}",
    )


def expected_plan_artifact_uris(
    *,
    bucket: str,
    repo_name: str,
    run_id: str,
    folder_path: str,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> PlanArtifactKeys:
    if pr_number is not None and pointer_type is not None:
        folder_keys = build_folder_artifact_keys_for_run(
            repo_name=repo_name,
            run_id=run_id,
            folder_path=folder_path,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        return PlanArtifactKeys(
            plan=f"s3://{bucket}/{folder_keys.plan_tfplan}",
            checksum=f"s3://{bucket}/{folder_keys.plan_sha256}",
            metadata=f"s3://{bucket}/{folder_keys.plan_metadata}",
        )
    keys = build_plan_artifact_keys(
        repo_name=repo_name, run_id=run_id, folder_path=folder_path
    )
    return PlanArtifactKeys(
        plan=f"s3://{bucket}/{keys.plan}",
        checksum=f"s3://{bucket}/{keys.checksum}",
        metadata=f"s3://{bucket}/{keys.metadata}",
    )


def artifact_env_suffix(relative_name: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", relative_name.upper())


def run_scoped_plan_pointer(*, repo_name: str, run_id: str, folder_path: str) -> str:
    return artifact_key(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder_path,
        relative_name="tf/plan.tfplan",
    )


def latest_plan_pointer(*, repo_name: str, folder_path: str) -> str:
    return f"{latest_folder_prefix(repo_name=repo_name, folder_path=folder_path)}tf/plan.tfplan"


_MANAGED_POINTER_TYPES = frozenset(
    {"plan", "report", "report-all", "apply", "destroy"}
)
_POINTER_LINE = re.compile(r"^EXECUTION_ID=(?P<execution_id>[\w+=,.@-]+)\n\Z")


def _validate_pr_number(pr_number: int) -> int:
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1:
        raise ValueError("pr_number must be a positive integer")
    return pr_number


def pointer_type_for_action(action: str) -> str:
    if action in {"plan", "drift"}:
        return "plan"
    if action == "plan_destroy":
        return "destroy"
    if action == "report":
        return "report"
    if action in {"apply", "destroy"}:
        return action
    raise ValueError(f"unsupported pointer action: {action}")


def _validate_pointer_type(pointer_type: str) -> str:
    if pointer_type not in _MANAGED_POINTER_TYPES:
        raise ValueError(f"unsupported pointer type: {pointer_type}")
    return pointer_type


def pr_pointer_key(
    *, repo_name: str, pr_number: int, folder_path: str, pointer_type: str
) -> str:
    """Return PR-scoped pointer object key for one folder/type pair."""
    repo = _validate_literal_segment(repo_name, label="repo_name")
    pr = _validate_pr_number(pr_number)
    folder = _validate_literal_segment(folder_path, label="folder_path")
    ptype = _validate_pointer_type(pointer_type)
    return f"{OPENCI_TF_PREFIX}/{repo}/pr-{pr}/{folder}/{ptype}.env"


def report_all_pointer_key(*, repo_name: str, pr_number: int) -> str:
    repo = _validate_literal_segment(repo_name, label="repo_name")
    pr = _validate_pr_number(pr_number)
    return f"{OPENCI_TF_PREFIX}/{repo}/pr-{pr}/report-all.env"


def execution_artifact_prefix(
    *,
    repo_name: str,
    pr_number: int,
    execution_id: str,
    pointer_type: str,
    folder_path: str,
) -> str:
    """Return immutable execution artifact prefix ending with /."""
    from src.domain.engine.outer_execution_id import validate_outer_run_id

    repo = _validate_literal_segment(repo_name, label="repo_name")
    pr = _validate_pr_number(pr_number)
    run = validate_outer_run_id(execution_id)
    folder = _validate_literal_segment(folder_path, label="folder_path")
    ptype = _validate_pointer_type(pointer_type)
    return (
        f"{OPENCI_TF_PREFIX}/{repo}/pr-{pr}/executions/{run}/{ptype}/{folder}/"
    )


def serialize_execution_pointer(execution_id: str) -> bytes:
    from src.domain.engine.outer_execution_id import validate_outer_run_id

    run = validate_outer_run_id(execution_id)
    return f"EXECUTION_ID={run}\n".encode("utf-8")


def parse_execution_pointer(body: str) -> str:
    if not isinstance(body, str):
        raise ValueError("pointer body must be a string")
    if "\n" in body.strip("\n"):
        extra = body.splitlines()
        if len(extra) != 1:
            raise ValueError("pointer body must contain exactly one assignment line")
    match = _POINTER_LINE.fullmatch(body if body.endswith("\n") else f"{body}\n")
    if match is None:
        raise ValueError("pointer body must be exactly EXECUTION_ID=<id>")
    from src.domain.engine.outer_execution_id import validate_outer_run_id

    return validate_outer_run_id(match.group("execution_id"))


def folder_artifact_prefix_for_run(
    *,
    repo_name: str,
    run_id: str,
    folder_path: str,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> str:
    """Prefer PR-scoped immutable execution prefixes when PR context exists."""
    if pr_number is not None and pointer_type is not None:
        return execution_artifact_prefix(
            repo_name=repo_name,
            pr_number=pr_number,
            execution_id=run_id,
            pointer_type=pointer_type,
            folder_path=folder_path,
        )
    return folder_artifact_prefix(
        repo_name=repo_name, run_id=run_id, folder_path=folder_path
    )


def build_folder_artifact_keys_for_run(
    *,
    repo_name: str,
    run_id: str,
    folder_path: str,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> FolderArtifactKeys:
    prefix = folder_artifact_prefix_for_run(
        repo_name=repo_name,
        run_id=run_id,
        folder_path=folder_path,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    return FolderArtifactKeys(
        prefix=prefix,
        init_out=f"{prefix}init.out",
        validate_out=f"{prefix}validate.out",
        plan_out=f"{prefix}tf/plan.out",
        drift_json=f"{prefix}drift.json",
        tfsec_json=f"{prefix}tfsec.json",
        tfsec_output=f"{prefix}tfsec.output",
        infracost_json=f"{prefix}infracost.json",
        infracost_output=f"{prefix}infracost.output",
        manifest_json=f"{prefix}manifest.json",
        plan_tfplan=f"{prefix}tf/plan.tfplan",
        plan_sha256=f"{prefix}tf/plan.tfplan.sha256",
        plan_metadata=f"{prefix}tf/plan-metadata.json",
        destroy_plan_out=f"{prefix}destroy.plan.out",
        destroy_plan_tfplan=f"{prefix}tf/destroy.plan.tfplan",
        destroy_plan_sha256=f"{prefix}tf/destroy.plan.tfplan.sha256",
        destroy_plan_metadata=f"{prefix}tf/destroy-plan-metadata.json",
    )


def manifest_tmp_copy_keys(
    manifest: dict,
    *,
    tmp_bucket: str,
    folder_prefix: str,
) -> tuple[str, ...]:
    """Return distinct tmp-bucket keys from manifest entries under folder_prefix."""
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return ()
    keys: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uri = entry.get("s3_uri")
        if not isinstance(uri, str):
            continue
        match = _S3_URI.fullmatch(uri)
        if match is None or match.group(1) != tmp_bucket:
            continue
        key = match.group(2)
        if key.startswith(folder_prefix):
            keys.append(key)
    return tuple(sorted(set(keys)))


def existing_manifest_tmp_copy_keys(
    manifest: dict,
    *,
    tmp_bucket: str,
    folder_prefix: str,
    object_exists,
) -> tuple[str, ...]:
    """Return manifest tmp keys that are present in the bucket."""
    return tuple(
        key
        for key in manifest_tmp_copy_keys(
            manifest,
            tmp_bucket=tmp_bucket,
            folder_prefix=folder_prefix,
        )
        if object_exists(tmp_bucket, key) is not None
    )
