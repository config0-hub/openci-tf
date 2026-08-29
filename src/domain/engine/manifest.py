# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded execution artifact manifest construction."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.terminal_evidence import redact_and_bound_terminal_evidence
from src.domain.engine.artifact_limits import (
    MAX_BINARY_PLAN_BYTES,
    MAX_CHECKSUM_SIDECAR_BYTES,
    MAX_DONE_MARKER_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PLAN_METADATA_BYTES,
)
from src.domain.engine.artifact_paths import (
    FolderArtifactKeys,
    build_folder_artifact_keys,
    build_folder_artifact_keys_for_run,
    expected_destroy_plan_artifact_uris,
    expected_plan_artifact_uris,
    manifest_key,
    pointer_type_for_action,
)
from src.domain.engine.lifecycle import (
    conservative_api_expiry_iso,
    done_retention_days,
    package_retention_days,
    tmp_retention_days,
)
from src.domain.engine.plan_artifacts import (
    plan_retention_days,
    validate_plan_artifact_metadata,
)
from src.domain.engine.manifest_schema import (
    _ALLOWED_ENTRY_KEYS,
    _ALLOWED_ENTRY_NAMES,
    _ALLOWED_TOP_LEVEL_KEYS,
    _DONE_CONTENT_TYPES,
    _ENTRY_MAX_BYTES,
    _ENTRY_MIN_BYTES,
    _FAILURE_APPLY_ALLOWED,
    _FAILURE_DESTROY_ALLOWED,
    _FAILURE_DRIFT_ALLOWED,
    _FAILURE_PLAN_DESTROY_ALLOWED,
    _FAILURE_PLAN_REPORT_ALLOWED,
    _MAX_MANIFEST_ENTRIES,
    _PACKAGE_CONTENT_TYPES,
    _SUCCESS_APPLY_ENTRIES,
    _SUCCESS_DESTROY_ENTRIES,
    _SUCCESS_DRIFT_ENTRIES,
    _SUCCESS_PLAN_DESTROY_ENTRIES,
    _SUCCESS_PLAN_REPORT_ENTRIES,
    _OPTIONAL_PLAN_REPORT_ENTRIES,
    _TEXT_ARTIFACTS,
)

_S3_URI = re.compile(r"^s3://([^/]+)/(.+)$")
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _strict_non_negative_int(value: object, *, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _iso_z(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _generated_at_from_metadata(*timestamps: datetime | None) -> str:
    candidates = [ts for ts in timestamps if isinstance(ts, datetime)]
    if not candidates:
        raise ValueError("generated_at requires object metadata timestamps")
    return _iso_z(max(candidates))


def _expiry_from_modified(last_modified: datetime, days: int) -> str:
    return conservative_api_expiry_iso(last_modified, days)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _last_modified(meta: dict[str, Any]) -> datetime:
    modified = meta.get("last_modified")
    if isinstance(modified, datetime):
        return modified
    raise ValueError("object metadata missing last_modified")


def _content_checksum(
    meta: dict[str, Any],
    *,
    bucket: str,
    key: str,
    read_object_bytes: Callable[[str, str, int], bytes | None],
    max_bytes: int,
) -> str:
    checksum = meta.get("checksum_sha256")
    if isinstance(checksum, str) and _SHA256.fullmatch(checksum):
        return checksum
    size = int(meta.get("content_length") or 0)
    if size > max_bytes:
        raise ValueError(f"object exceeds {max_bytes} bytes: s3://{bucket}/{key}")
    body = read_object_bytes(bucket, key, max_bytes)
    if body is None:
        raise ValueError(f"unable to read object bytes for checksum: s3://{bucket}/{key}")
    return _sha256_bytes(body)


def _entry(
    name: str,
    uri: str,
    content_type: str,
    *,
    size: int,
    checksum: str,
    expires_at: str,
) -> dict[str, Any]:
    if not _SHA256.fullmatch(checksum):
        raise ValueError(f"manifest entry {name} has invalid checksum")
    if not _ISO_Z.fullmatch(expires_at):
        raise ValueError(f"manifest entry {name} has invalid expires_at")
    return {
        "name": name,
        "s3_uri": uri,
        "content_type": content_type,
        "size": size,
        "checksum": checksum,
        "expires_at": expires_at,
    }


def _canonical_manifest_digest(manifest: dict[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    match = _S3_URI.fullmatch(uri)
    if not match:
        raise ValueError(f"invalid s3 uri: {uri}")
    return match.group(1), match.group(2)


def _artifact_names_for_action(action: str) -> tuple[str, ...]:
    if action == "drift":
        return ("init.out", "validate.out", "tf/plan.out", "drift.json")
    if action == "plan_destroy":
        return ("init.out", "validate.out", "destroy.plan.out")
    if action == "apply":
        return ("init.out", "validate.out", "plan-show.out", "apply.out")
    if action == "destroy":
        return ("init.out", "validate.out", "plan-show.out", "destroy.out")
    return ("init.out", "validate.out", "tf/plan.out", "tfsec.json", "tfsec.output", "infracost.json")


def _expected_done_uri(done_bucket: str, execution_id: str) -> str:
    return f"s3://{done_bucket}/{execution_id}/done"


def _expected_package_uri(package_bucket: str, execution_id: str) -> str:
    return f"s3://{package_bucket}/{execution_id}.zip"


def _head_content_type(meta: dict[str, Any], default: str) -> str:
    raw = meta.get("content_type")
    if isinstance(raw, str) and raw:
        return raw.split(";", 1)[0]
    return default


def _bound_failure_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    text = reason.strip()
    if not text:
        return None
    bounded = redact_and_bound_terminal_evidence(text)
    if not isinstance(bounded, str):
        raise TypeError("terminal failure redactor must return a string")
    return bounded


def _tmp_entry_expiry(meta: dict[str, Any], key: str) -> str:
    days = plan_retention_days() if key.startswith("openci-tf/") else tmp_retention_days()
    return _expiry_from_modified(_last_modified(meta), days)


def _required_binding_fields(manifest: dict[str, Any]) -> None:
    required = ("run_id", "repo_name", "commit_hash", "account_id", "folder", "action", "attempt")
    missing = [field for field in required if manifest.get(field) in {None, ""}]
    if missing:
        raise ValueError(f"manifest missing binding fields: {', '.join(missing)}")
    if ("pr_number" in manifest) != ("pointer_type" in manifest):
        raise ValueError("manifest PR binding requires both pr_number and pointer_type")
    if "pr_number" in manifest:
        pr_number = _strict_non_negative_int(manifest.get("pr_number"), label="manifest pr_number")
        if pr_number <= 0:
            raise ValueError("manifest pr_number must be positive")
        if not isinstance(manifest.get("pointer_type"), str) or not manifest["pointer_type"]:
            raise ValueError("manifest pointer_type must be non-empty when pr_number is set")


def _required_top_level_fields(manifest: dict[str, Any]) -> None:
    required = {
        "version",
        "execution_id",
        "action",
        "generated_at",
        "manifest_s3_uri",
        "entries",
        "package_bucket",
        "tmp_bucket",
        "done_bucket",
        "plan_retention_days",
        "manifest_sha256",
        "run_id",
        "repo_name",
        "commit_hash",
        "account_id",
        "folder",
        "attempt",
    }
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"manifest missing fields: {', '.join(sorted(missing))}")
    entries = manifest.get("entries")
    if isinstance(entries, list) and not entries and not manifest.get("failure_reason"):
        raise ValueError("manifest missing failure_reason for terminal failure")


def _folder_keys(
    manifest: dict[str, Any],
    *,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> FolderArtifactKeys:
    resolved_pr_number = pr_number
    resolved_pointer_type = pointer_type
    if resolved_pr_number is None and isinstance(manifest.get("pr_number"), int):
        resolved_pr_number = int(manifest["pr_number"])
    if resolved_pointer_type is None and isinstance(manifest.get("pointer_type"), str):
        resolved_pointer_type = str(manifest["pointer_type"])
    if resolved_pr_number is not None and resolved_pointer_type is not None:
        return build_folder_artifact_keys_for_run(
            repo_name=str(manifest["repo_name"]),
            run_id=str(manifest["run_id"]),
            folder_path=str(manifest["folder"]),
            pr_number=resolved_pr_number,
            pointer_type=resolved_pointer_type,
        )
    return build_folder_artifact_keys(
        repo_name=str(manifest["repo_name"]),
        run_id=str(manifest["run_id"]),
        folder_path=str(manifest["folder"]),
    )


def _expected_manifest_key(
    manifest: dict[str, Any],
    *,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> str:
    resolved_pr_number = pr_number
    resolved_pointer_type = pointer_type
    if resolved_pr_number is None and isinstance(manifest.get("pr_number"), int):
        resolved_pr_number = int(manifest["pr_number"])
    if resolved_pointer_type is None and isinstance(manifest.get("pointer_type"), str):
        resolved_pointer_type = str(manifest["pointer_type"])
    if resolved_pr_number is not None and resolved_pointer_type is None:
        resolved_pointer_type = pointer_type_for_action(str(manifest.get("action") or ""))
    return manifest_key(
        str(manifest["repo_name"]),
        str(manifest["run_id"]),
        str(manifest["folder"]),
        pr_number=resolved_pr_number,
        pointer_type=resolved_pointer_type if resolved_pr_number is not None else None,
    )


def _expected_entry_uri(
    name: str,
    manifest: dict[str, Any],
    *,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> str:
    exec_id = str(manifest["execution_id"])
    tmp_bucket = str(manifest["tmp_bucket"])
    done_bucket = str(manifest["done_bucket"])
    package_bucket = str(manifest["package_bucket"])
    keys = _folder_keys(manifest, pr_number=pr_number, pointer_type=pointer_type)
    key_by_name = {
        "init.out": keys.init_out,
        "validate.out": keys.validate_out,
        "tf/plan.out": keys.plan_out,
        "drift.json": keys.drift_json,
        "tfsec.json": keys.tfsec_json,
        "tfsec.output": keys.tfsec_output,
        "infracost.json": keys.infracost_json,
        "infracost.output": keys.infracost_output,
        "destroy.plan.out": keys.destroy_plan_out,
        "apply.out": f"{keys.prefix}apply.out",
        "plan-show.out": f"{keys.prefix}plan-show.out",
        "destroy.out": f"{keys.prefix}destroy.out",
    }
    if name in key_by_name:
        return f"s3://{tmp_bucket}/{key_by_name[name]}"
    if name == "done":
        return _expected_done_uri(done_bucket, exec_id)
    if name == "package":
        return _expected_package_uri(package_bucket, exec_id)
    if name in {"plan.tfplan", "plan.tfplan.sha256", "plan-metadata.json"}:
        expected = expected_plan_artifact_uris(
            bucket=tmp_bucket,
            repo_name=str(manifest["repo_name"]),
            run_id=str(manifest["run_id"]),
            folder_path=str(manifest["folder"]),
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        if name == "plan.tfplan":
            return expected.plan
        if name == "plan.tfplan.sha256":
            return expected.checksum
        return expected.metadata
    if name in {"destroy.plan.tfplan", "destroy.plan.tfplan.sha256", "destroy-plan-metadata.json"}:
        expected_destroy = expected_destroy_plan_artifact_uris(
            bucket=tmp_bucket,
            repo_name=str(manifest["repo_name"]),
            run_id=str(manifest["run_id"]),
            folder_path=str(manifest["folder"]),
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        if name == "destroy.plan.tfplan":
            return expected_destroy.plan
        if name == "destroy.plan.tfplan.sha256":
            return expected_destroy.checksum
        return expected_destroy.metadata
    raise ValueError(f"manifest entry name not allowed: {name!r}")


def _validate_entry_topology(
    name: str,
    uri: str,
    manifest: dict[str, Any],
    *,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> None:
    expected_uri = _expected_entry_uri(
        name, manifest, pr_number=pr_number, pointer_type=pointer_type
    )
    if uri != expected_uri:
        raise ValueError(f"manifest entry {name} must use exact binding URI {expected_uri}, got {uri}")


def _validate_entry_class(name: str, content_type: str, size: int) -> None:
    _strict_non_negative_int(size, label=f"manifest entry {name} size")
    min_bytes = _ENTRY_MIN_BYTES.get(name)
    if min_bytes is not None and size < min_bytes:
        raise ValueError(f"manifest entry {name} below minimum size")
    expected_type = _TEXT_ARTIFACTS.get(name)
    if expected_type and content_type != expected_type:
        raise ValueError(f"manifest entry {name} requires {expected_type} content type")
    if name == "plan-metadata.json" and content_type != "application/json":
        raise ValueError("manifest entry plan-metadata.json requires application/json content type")
    if name == "destroy-plan-metadata.json" and content_type != "application/json":
        raise ValueError("manifest entry destroy-plan-metadata.json requires application/json content type")
    if name == "done" and content_type not in _DONE_CONTENT_TYPES:
        raise ValueError("manifest entry done requires binary/octet-stream or application/octet-stream content type")
    if name == "plan.tfplan" and content_type != "application/octet-stream":
        raise ValueError("manifest entry plan.tfplan requires application/octet-stream content type")
    if name == "plan.tfplan.sha256" and content_type != "text/plain":
        raise ValueError("manifest entry plan.tfplan.sha256 requires text/plain content type")
    if name == "destroy.plan.tfplan" and content_type != "application/octet-stream":
        raise ValueError("manifest entry destroy.plan.tfplan requires application/octet-stream content type")
    if name == "destroy.plan.tfplan.sha256" and content_type != "text/plain":
        raise ValueError("manifest entry destroy.plan.tfplan.sha256 requires text/plain content type")
    if name == "package" and content_type not in _PACKAGE_CONTENT_TYPES:
        raise ValueError("manifest entry package requires application/octet-stream or application/zip content type")
    max_bytes = _ENTRY_MAX_BYTES.get(name)
    if max_bytes is not None and size > max_bytes:
        raise ValueError(f"manifest entry {name} exceeds size bound")


def validate_manifest_schema(
    manifest: dict[str, Any],
    *,
    execution_id: str | None = None,
    pr_number: int | None = None,
    pointer_type: str | None = None,
) -> None:
    unknown = sorted(set(manifest) - _ALLOWED_TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"manifest has unknown fields: {', '.join(unknown)}")
    _required_top_level_fields(manifest)
    if manifest.get("version") != 1:
        raise ValueError("unsupported manifest version")
    if execution_id is not None and manifest.get("execution_id") != execution_id:
        raise ValueError("manifest execution_id mismatch")
    configured_retention = plan_retention_days()
    manifest_retention = _strict_non_negative_int(
        manifest.get("plan_retention_days"),
        label="manifest plan_retention_days",
    )
    if manifest_retention != configured_retention:
        raise ValueError("manifest plan_retention_days mismatch")
    _required_binding_fields(manifest)
    _strict_non_negative_int(manifest.get("attempt"), label="manifest attempt")
    expected_manifest_uri = (
        f"s3://{manifest['tmp_bucket']}/"
        f"{_expected_manifest_key(manifest, pr_number=pr_number, pointer_type=pointer_type)}"
    )
    if manifest.get("manifest_s3_uri") != expected_manifest_uri:
        raise ValueError("manifest_s3_uri does not match expected execution topology")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise TypeError("manifest entries must be a list")
    if len(entries) > _MAX_MANIFEST_ENTRIES:
        raise ValueError("manifest entries exceed bound")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("manifest entry must be an object")
        unknown_entry = sorted(set(entry) - _ALLOWED_ENTRY_KEYS)
        if unknown_entry:
            raise ValueError(f"manifest entry has unknown fields: {', '.join(unknown_entry)}")
        name = entry.get("name")
        if not isinstance(name, str) or name not in _ALLOWED_ENTRY_NAMES:
            raise ValueError(f"manifest entry name not allowed: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate manifest entry: {name}")
        seen.add(name)
        uri = entry.get("s3_uri")
        content_type = entry.get("content_type")
        size = entry.get("size")
        if not isinstance(uri, str) or not uri.startswith("s3://"):
            raise ValueError(f"manifest entry {name} has invalid s3_uri")
        if not isinstance(content_type, str) or not content_type:
            raise ValueError(f"manifest entry {name} missing content_type")
        size = _strict_non_negative_int(size, label=f"manifest entry {name} size")
        _validate_entry_topology(
            name, uri, manifest, pr_number=pr_number, pointer_type=pointer_type
        )
        _validate_entry_class(name, content_type.split(";", 1)[0], size)
        checksum = entry.get("checksum")
        expires_at = entry.get("expires_at")
        if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
            raise ValueError(f"manifest entry {name} missing checksum")
        if not isinstance(expires_at, str) or not _ISO_Z.fullmatch(expires_at):
            raise ValueError(f"manifest entry {name} missing expires_at")
    _validate_required_entry_set(manifest)
    action = str(manifest.get("action") or "")
    if action in {"apply", "destroy"}:
        source_plan_run_id = manifest.get("source_plan_run_id")
        if not isinstance(source_plan_run_id, str) or not source_plan_run_id:
            raise ValueError("manifest missing source_plan_run_id for mutation action")
    digest = _canonical_manifest_digest(manifest)
    if manifest.get("manifest_sha256") != digest:
        raise ValueError("manifest digest mismatch")


def _validate_required_entry_set(manifest: dict[str, Any]) -> None:
    action = str(manifest.get("action") or "")
    names: set[str] = set()
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str):
            names.add(name)
    if manifest.get("failure_reason"):
        if action in {"plan", "report"}:
            allowed = _FAILURE_PLAN_REPORT_ALLOWED
        elif action == "drift":
            allowed = _FAILURE_DRIFT_ALLOWED
        elif action == "plan_destroy":
            allowed = _FAILURE_PLAN_DESTROY_ALLOWED
        elif action == "apply":
            allowed = _FAILURE_APPLY_ALLOWED
        elif action == "destroy":
            allowed = _FAILURE_DESTROY_ALLOWED
        else:
            raise ValueError(f"unsupported manifest action: {action}")
        extra = sorted(names - allowed)
        if extra:
            raise ValueError(f"manifest has unexpected entries for failed {action}: {', '.join(extra)}")
        return
    if action in {"plan", "report"}:
        required = _SUCCESS_PLAN_REPORT_ENTRIES
    elif action == "drift":
        required = _SUCCESS_DRIFT_ENTRIES
    elif action == "plan_destroy":
        required = _SUCCESS_PLAN_DESTROY_ENTRIES
    elif action == "apply":
        required = _SUCCESS_APPLY_ENTRIES
    elif action == "destroy":
        required = _SUCCESS_DESTROY_ENTRIES
    else:
        raise ValueError(f"unsupported manifest action: {action}")
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"manifest missing required entries for {action}: {', '.join(missing)}")
    extra = sorted(names - required - _OPTIONAL_PLAN_REPORT_ENTRIES)
    if extra:
        raise ValueError(f"manifest has unexpected entries for {action}: {', '.join(extra)}")


def validate_manifest_binding(
    manifest: dict[str, Any],
    *,
    run_id: str,
    repo_name: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    action: str,
    attempt: int,
) -> None:
    if manifest.get("run_id") != run_id:
        raise ValueError("manifest run_id mismatch")
    if manifest.get("repo_name") != repo_name:
        raise ValueError("manifest repo_name mismatch")
    if manifest.get("commit_hash") != commit_hash:
        raise ValueError("manifest commit_hash mismatch")
    if manifest.get("account_id") != account_id:
        raise ValueError("manifest account_id mismatch")
    if manifest.get("folder") != folder:
        raise ValueError("manifest folder mismatch")
    if manifest.get("action") != action:
        raise ValueError("manifest action mismatch")
    manifest_attempt = _strict_non_negative_int(manifest.get("attempt"), label="manifest attempt")
    if manifest_attempt != _strict_non_negative_int(attempt, label="manifest attempt"):
        raise ValueError("manifest attempt mismatch")


@dataclass(frozen=True)
class ManifestBinding:
    """Identity/run context a manifest is bound to."""

    run_id: str | None = None
    repo_name: str | None = None
    commit_hash: str | None = None
    account_id: str | None = None
    folder: str | None = None
    attempt: int | None = None
    source_plan_run_id: str | None = None
    pr_number: int | None = None
    pointer_type: str | None = None


@dataclass(frozen=True)
class BucketSet:
    """Bucket names and expected transfer-object URIs for one execution."""

    tmp_bucket: str
    done_bucket: str
    package_bucket: str
    done_uri: str
    package_uri: str | None = None


def _build_artifact_entries(
    *,
    action: str,
    tmp_bucket: str,
    keys: FolderArtifactKeys,
    head_object: Callable[[str, str], dict[str, Any] | None],
    read_object_bytes: Callable[[str, str, int], bytes | None],
    require_complete: bool,
    entries: list[dict[str, Any]],
    generated_timestamps: list[datetime],
) -> None:
    """Append one validated manifest entry per expected per-action tmp artifact."""
    key_by_name = {
        "init.out": keys.init_out,
        "validate.out": keys.validate_out,
        "tf/plan.out": keys.plan_out,
        "drift.json": keys.drift_json,
        "tfsec.json": keys.tfsec_json,
        "tfsec.output": keys.tfsec_output,
        "infracost.json": keys.infracost_json,
        "infracost.output": keys.infracost_output,
        "destroy.plan.out": keys.destroy_plan_out,
        "apply.out": f"{keys.prefix}apply.out",
        "plan-show.out": f"{keys.prefix}plan-show.out",
        "destroy.out": f"{keys.prefix}destroy.out",
    }
    for name in _artifact_names_for_action(action):
        key = key_by_name[name]
        uri = f"s3://{tmp_bucket}/{key}"
        meta = head_object(tmp_bucket, key)
        if meta is None:
            if require_complete:
                raise ValueError(f"expected artifact missing: {name}")
            continue
        generated_timestamps.append(_last_modified(meta))
        entries.append(
            _entry(
                name,
                uri,
                _head_content_type(meta, _TEXT_ARTIFACTS[name]),
                size=int(meta["content_length"]),
                checksum=_content_checksum(
                    meta,
                    bucket=tmp_bucket,
                    key=key,
                    read_object_bytes=read_object_bytes,
                    max_bytes=MAX_DONE_MARKER_BYTES,
                ),
                expires_at=_tmp_entry_expiry(meta, key),
            )
        )
    if action in {"plan", "report"}:
        optional_name = "infracost.output"
        optional_key = keys.infracost_output
        optional_uri = f"s3://{tmp_bucket}/{optional_key}"
        optional_meta = head_object(tmp_bucket, optional_key)
        if optional_meta is not None:
            generated_timestamps.append(_last_modified(optional_meta))
            entries.append(
                _entry(
                    optional_name,
                    optional_uri,
                    _head_content_type(optional_meta, _TEXT_ARTIFACTS[optional_name]),
                    size=int(optional_meta["content_length"]),
                    checksum=_content_checksum(
                        optional_meta,
                        bucket=tmp_bucket,
                        key=optional_key,
                        read_object_bytes=read_object_bytes,
                        max_bytes=MAX_DONE_MARKER_BYTES,
                    ),
                    expires_at=_tmp_entry_expiry(optional_meta, optional_key),
                )
            )


def build_manifest(
    *,
    execution_id: str,
    buckets: BucketSet,
    binding: ManifestBinding,
    action: str,
    head_object: Callable[[str, str], dict[str, Any] | None],
    read_object_bytes: Callable[[str, str, int], bytes | None],
    plan_metadata: dict[str, Any] | None,
    plan_dimensions: dict[str, Any] | None = None,
    failure_reason: str | None = None,
    generated_at_source: datetime | None = None,
    folder_keys: FolderArtifactKeys | None = None,
    manifest_object_key: str | None = None,
) -> dict[str, Any]:
    """Build one bounded manifest enumerating validated artifact pointers."""
    tmp_bucket = buckets.tmp_bucket
    done_bucket = buckets.done_bucket
    package_bucket = buckets.package_bucket
    done_uri = buckets.done_uri
    package_uri = buckets.package_uri
    run_id = binding.run_id
    repo_name = binding.repo_name
    commit_hash = binding.commit_hash
    account_id = binding.account_id
    folder = binding.folder
    attempt = binding.attempt
    source_plan_run_id = binding.source_plan_run_id
    pr_number = binding.pr_number
    pointer_type = binding.pointer_type
    if attempt is not None:
        _strict_non_negative_int(attempt, label="manifest attempt")
    generated_timestamps: list[datetime] = []
    if isinstance(generated_at_source, datetime):
        generated_timestamps.append(generated_at_source)
    entries: list[dict[str, Any]] = []
    keys = folder_keys or build_folder_artifact_keys(
        repo_name=str(repo_name or ""),
        run_id=str(run_id or ""),
        folder_path=str(folder or ""),
    )
    require_complete = failure_reason is None
    _build_artifact_entries(
        action=action,
        tmp_bucket=tmp_bucket,
        keys=keys,
        head_object=head_object,
        read_object_bytes=read_object_bytes,
        require_complete=require_complete,
        entries=entries,
        generated_timestamps=generated_timestamps,
    )
    expected_done = _expected_done_uri(done_bucket, execution_id)
    if done_uri != expected_done:
        raise ValueError("done uri does not match expected execution path")
    if require_complete:
        done_bucket_name, done_key = _parse_s3_uri(done_uri)
        done_meta = head_object(done_bucket_name, done_key)
        if done_meta is None:
            raise ValueError("expected done marker missing")
        generated_timestamps.append(_last_modified(done_meta))
        entries.append(
            _entry(
                "done",
                done_uri,
                _head_content_type(done_meta, "binary/octet-stream"),
                size=int(done_meta["content_length"]),
                checksum=_content_checksum(
                    done_meta,
                    bucket=done_bucket_name,
                    key=done_key,
                    read_object_bytes=read_object_bytes,
                    max_bytes=MAX_DONE_MARKER_BYTES,
                ),
                expires_at=_expiry_from_modified(_last_modified(done_meta), done_retention_days()),
            )
        )
    if require_complete and package_uri:
        expected_package = _expected_package_uri(package_bucket, execution_id)
        if package_uri != expected_package:
            raise ValueError("package uri does not match expected execution path")
        package_bucket_name, package_object_key = _parse_s3_uri(package_uri)
        package_meta = head_object(package_bucket_name, package_object_key)
        if package_meta is None:
            raise ValueError("expected package missing")
        generated_timestamps.append(_last_modified(package_meta))
        entries.append(
            _entry(
                "package",
                package_uri,
                _head_content_type(package_meta, "application/octet-stream"),
                size=int(package_meta["content_length"]),
                checksum=_content_checksum(
                    package_meta,
                    bucket=package_bucket_name,
                    key=package_object_key,
                    read_object_bytes=read_object_bytes,
                    max_bytes=MAX_PACKAGE_BYTES,
                ),
                expires_at=_expiry_from_modified(_last_modified(package_meta), package_retention_days()),
            )
        )
    elif require_complete:
        raise ValueError("package uri required for successful manifest")
    if require_complete and action in {"plan", "report", "plan_destroy"} and not plan_metadata:
        raise ValueError("plan metadata required for successful plan/report/plan_destroy manifest")
    if require_complete and plan_metadata and plan_dimensions and action in {"plan", "report", "plan_destroy"}:
        repo = str(plan_dimensions.get("repo_name") or "")
        sha = str(plan_dimensions.get("commit_hash") or "")
        account = str(plan_dimensions.get("account_id") or "")
        folder_path = str(plan_dimensions.get("folder") or "")
        bound_run_id = str(plan_dimensions.get("run_id") or run_id or "")
        if action == "plan_destroy":
            expected = expected_destroy_plan_artifact_uris(
                bucket=tmp_bucket,
                repo_name=repo,
                run_id=bound_run_id,
                folder_path=folder_path,
                pr_number=pr_number,
                pointer_type=pointer_type,
            )
        else:
            expected = expected_plan_artifact_uris(
                bucket=tmp_bucket,
                repo_name=repo,
                run_id=bound_run_id,
                folder_path=folder_path,
                pr_number=pr_number,
                pointer_type=pointer_type,
            )
        metadata_uri = str(plan_metadata.get("metadata_s3_uri") or "")
        if metadata_uri != expected.metadata:
            raise ValueError("plan metadata uri does not match expected canonical key")
        validated = validate_plan_artifact_metadata(
            metadata=plan_metadata,
            bucket=tmp_bucket,
            repo_name=repo,
            run_id=bound_run_id,
            commit_hash=sha,
            account_id=account,
            folder=folder_path,
            action=action,
            pr_number=pr_number,
            pointer_type=pointer_type,
        )
        plan_uri = str(validated["plan_s3_uri"])
        plan_digest = str(validated["sha256"])
        plan_expires_at = str(validated["expires_at"])
        plan_entry_name = "destroy.plan.tfplan" if action == "plan_destroy" else "plan.tfplan"
        sha_entry_name = "destroy.plan.tfplan.sha256" if action == "plan_destroy" else "plan.tfplan.sha256"
        meta_entry_name = "destroy-plan-metadata.json" if action == "plan_destroy" else "plan-metadata.json"
        plan_bucket, plan_key = _parse_s3_uri(plan_uri)
        plan_meta = head_object(plan_bucket, plan_key)
        if plan_meta is None:
            if require_complete:
                raise ValueError("expected plan artifact missing")
        else:
            generated_timestamps.append(_last_modified(plan_meta))
            body_checksum = _content_checksum(
                plan_meta,
                bucket=plan_bucket,
                key=plan_key,
                read_object_bytes=read_object_bytes,
                max_bytes=MAX_BINARY_PLAN_BYTES,
            )
            if body_checksum != plan_digest and require_complete:
                raise ValueError("binary plan checksum does not match metadata")
            entries.append(
                _entry(
                    plan_entry_name,
                    plan_uri,
                    _head_content_type(plan_meta, "application/octet-stream"),
                    size=int(plan_meta["content_length"]),
                    checksum=plan_digest,
                    expires_at=plan_expires_at,
                )
            )
        sha_uri = str(validated["sha256_s3_uri"])
        sha_bucket, sha_key = _parse_s3_uri(sha_uri)
        sha_meta = head_object(sha_bucket, sha_key)
        if sha_meta is None:
            if require_complete:
                raise ValueError("expected plan checksum sidecar missing")
        else:
            generated_timestamps.append(_last_modified(sha_meta))
            sidecar_body = read_object_bytes(sha_bucket, sha_key, MAX_CHECKSUM_SIDECAR_BYTES)
            if sidecar_body is None:
                raise ValueError("unable to read plan checksum sidecar body")
            sidecar_text = sidecar_body.decode("utf-8", errors="replace").strip()
            if sidecar_text != plan_digest and require_complete:
                raise ValueError("plan checksum sidecar does not match metadata")
            sidecar_checksum = _sha256_bytes(sidecar_body)
            entries.append(
                _entry(
                    sha_entry_name,
                    sha_uri,
                    _head_content_type(sha_meta, "text/plain"),
                    size=int(sha_meta["content_length"]),
                    checksum=sidecar_checksum,
                    expires_at=plan_expires_at,
                )
            )
        meta_bucket, meta_key = _parse_s3_uri(expected.metadata)
        meta_head = head_object(meta_bucket, meta_key)
        if meta_head is None:
            if require_complete:
                raise ValueError("expected plan metadata sidecar missing")
        else:
            generated_timestamps.append(_last_modified(meta_head))
            entries.append(
                _entry(
                    meta_entry_name,
                    expected.metadata,
                    _head_content_type(meta_head, "application/json"),
                    size=int(meta_head["content_length"]),
                    checksum=_content_checksum(
                        meta_head,
                        bucket=meta_bucket,
                        key=meta_key,
                        read_object_bytes=read_object_bytes,
                        max_bytes=MAX_PLAN_METADATA_BYTES,
                    ),
                    expires_at=plan_expires_at,
                )
            )
    if generated_timestamps:
        generated_at = _generated_at_from_metadata(*generated_timestamps)
    elif isinstance(generated_at_source, datetime):
        generated_at = _iso_z(generated_at_source)
    else:
        generated_at = _iso_z(datetime.now(timezone.utc))
    manifest: dict[str, Any] = {
        "version": 1,
        "execution_id": execution_id,
        "action": action,
        "generated_at": generated_at,
        "manifest_s3_uri": (
            f"s3://{tmp_bucket}/{manifest_object_key}"
            if manifest_object_key
            else f"s3://{tmp_bucket}/{manifest_key(str(repo_name or ''), str(run_id or ''), str(folder or ''))}"
        ),
        "entries": entries[:_MAX_MANIFEST_ENTRIES],
        "package_bucket": package_bucket,
        "tmp_bucket": tmp_bucket,
        "done_bucket": done_bucket,
        "plan_retention_days": plan_retention_days(),
    }
    if run_id is not None:
        manifest["run_id"] = run_id
    if repo_name is not None:
        manifest["repo_name"] = repo_name
    if commit_hash is not None:
        manifest["commit_hash"] = commit_hash
    if account_id is not None:
        manifest["account_id"] = account_id
    if folder is not None:
        manifest["folder"] = folder
    if attempt is not None:
        manifest["attempt"] = attempt
    if source_plan_run_id is not None:
        manifest["source_plan_run_id"] = source_plan_run_id
    if pr_number is not None:
        manifest["pr_number"] = pr_number
    if pointer_type is not None:
        manifest["pointer_type"] = pointer_type
    bounded_failure = _bound_failure_reason(failure_reason)
    if bounded_failure:
        manifest["failure_reason"] = bounded_failure
    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)
    validate_manifest_schema(
        manifest,
        execution_id=execution_id,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    encoded = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds bounded size")
    return manifest


def build_failure_manifest(
    *,
    execution_id: str,
    tmp_bucket: str,
    done_bucket: str,
    package_bucket: str,
    action: str,
    failure_reason: str,
    run_id: str,
    repo_name: str,
    commit_hash: str,
    account_id: str,
    folder: str,
    attempt: int,
    generated_at_source: datetime,
    source_plan_run_id: str | None = None,
    pr_number: int | None = None,
    pointer_type: str | None = None,
    manifest_object_key: str | None = None,
) -> dict[str, Any]:
    """Deterministic failure manifest for terminal inner failures before Collect."""
    _strict_non_negative_int(attempt, label="manifest attempt")
    generated_at = _generated_at_from_metadata(generated_at_source)
    manifest_key_value = manifest_object_key or manifest_key(
        str(repo_name or ""),
        str(run_id or ""),
        str(folder or ""),
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    manifest = {
        "version": 1,
        "execution_id": execution_id,
        "action": action,
        "generated_at": generated_at,
        "manifest_s3_uri": f"s3://{tmp_bucket}/{manifest_key_value}",
        "entries": [],
        "package_bucket": package_bucket,
        "tmp_bucket": tmp_bucket,
        "done_bucket": done_bucket,
        "plan_retention_days": plan_retention_days(),
        "run_id": run_id,
        "repo_name": repo_name,
        "commit_hash": commit_hash,
        "account_id": account_id,
        "folder": folder,
        "attempt": attempt,
        "failure_reason": _bound_failure_reason(failure_reason) or "execution failed",
    }
    if source_plan_run_id is not None:
        manifest["source_plan_run_id"] = source_plan_run_id
    if pr_number is not None:
        manifest["pr_number"] = pr_number
    if pointer_type is not None:
        manifest["pointer_type"] = pointer_type
    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)
    validate_manifest_schema(
        manifest,
        execution_id=execution_id,
        pr_number=pr_number,
        pointer_type=pointer_type,
    )
    return manifest


def validate_manifest_entry_name(name: str) -> bool:
    if not name or "/" in name or ".." in name:
        return False
    return name in _ALLOWED_ENTRY_NAMES
