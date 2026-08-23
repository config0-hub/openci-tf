# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict pipeline definition parsing."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from src.core.errors import ConfigResolutionError, ConfigValidationError
_PIPELINE_NAME = re.compile(r"^[A-Za-z0-9_./-]+$")
_ALLOWED_TOP_LEVEL_KEYS = frozenset({"steps"})
_ALLOWED_STEP_KEYS = frozenset({"folder", "parallel"})
_ALLOWED_PARALLEL_ENTRY_KEYS = frozenset({"folder"})
_MAX_PIPELINE_FOLDERS = 20
_PIPELINES_ROOT = Path(".openci_tf/pipelines")


@dataclass(frozen=True)
class Step:
    folders: tuple[str, ...]


@dataclass(frozen=True)
class Pipeline:
    name: str
    steps: tuple[Step, ...]


def parse_pipeline(text: str, *, name: str = "") -> Pipeline:
    """Parse a pipeline YAML document without touching the repository checkout."""
    data = yaml.safe_load(text)
    if data is None:
        raise ConfigValidationError("pipeline definition must be a mapping")
    if not isinstance(data, dict):
        raise ConfigValidationError("pipeline definition must be a mapping")
    _reject_nested_pipeline_key(data)
    _reject_unknown_keys(data, _ALLOWED_TOP_LEVEL_KEYS, "pipeline")
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ConfigValidationError("pipeline steps must be a non-empty list")

    steps: list[Step] = []
    seen: set[str] = set()
    total = 0
    for index, raw_step in enumerate(steps_raw, start=1):
        folders = _parse_step(raw_step, index=index)
        for folder in folders:
            if folder in seen:
                raise ConfigValidationError(f"duplicate pipeline folder: {folder}")
            seen.add(folder)
        total += len(folders)
        if total > _MAX_PIPELINE_FOLDERS:
            raise ConfigValidationError(
                f"pipeline exceeds maximum of {_MAX_PIPELINE_FOLDERS} folders"
            )
        steps.append(Step(folders=folders))
    return Pipeline(name=name, steps=tuple(steps))


def discover_pipelines(root: Path) -> dict[str, Path]:
    """Return safe pipeline names mapped to their YAML definition paths."""
    base = (root / _PIPELINES_ROOT).resolve()
    if not base.is_dir():
        return {}
    discovered: dict[str, Path] = {}
    for path in sorted(base.rglob("*.yaml")):
        resolved = path.resolve()
        if base not in resolved.parents:
            raise ConfigResolutionError("pipeline path escapes .openci_tf/pipelines")
        name = path.relative_to(base).with_suffix("").as_posix()
        _validate_pipeline_name(name)
        discovered[name] = resolved
    return discovered


def canonical_pipeline_sha256(pipeline: Pipeline) -> str:
    """Hash the canonical parsed pipeline shape used for apply step continuity."""
    payload = [[folder for folder in step.folders] for step in pipeline.steps]
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_pipeline(root: Path, name: str) -> Pipeline:
    """Load and validate one pipeline definition against configured folders."""
    safe_name = _validate_pipeline_name(name)
    base = (root / _PIPELINES_ROOT).resolve()
    path = (base / f"{safe_name}.yaml").resolve()
    if base not in path.parents:
        raise ConfigResolutionError("pipeline path escapes .openci_tf/pipelines")
    if not path.is_file():
        raise ConfigResolutionError(f"unknown pipeline: {safe_name}")
    try:
        pipeline = parse_pipeline(path.read_text(), name=safe_name)
    except OSError as error:
        raise ConfigResolutionError(f"unknown pipeline: {safe_name}") from error
    _validate_pipeline_folders(root, pipeline)
    return pipeline


def _reject_unknown_keys(
    data: dict[Any, Any], allowed: frozenset[str], label: str
) -> None:
    keys = {key for key in data if isinstance(key, str)}
    unknown = sorted(keys - allowed)
    non_string = [key for key in data if not isinstance(key, str)]
    if non_string:
        raise ConfigValidationError(f"{label} contains non-string keys")
    if unknown:
        raise ConfigValidationError(f"{label} has unknown keys: {', '.join(unknown)}")


def _reject_nested_pipeline_key(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "pipeline":
                raise ConfigValidationError("nested pipeline references are not supported")
            _reject_nested_pipeline_key(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nested_pipeline_key(item)


def _parse_step(raw_step: object, *, index: int) -> tuple[str, ...]:
    if not isinstance(raw_step, dict):
        raise ConfigValidationError(f"pipeline step {index} must be a mapping")
    _reject_unknown_keys(raw_step, _ALLOWED_STEP_KEYS, f"pipeline step {index}")
    if "folder" in raw_step and "parallel" in raw_step:
        raise ConfigValidationError(
            f"pipeline step {index} must contain exactly one of folder or parallel"
        )
    if "folder" in raw_step:
        return (_validate_folder_value(raw_step["folder"], label=f"pipeline step {index}.folder"),)
    if "parallel" in raw_step:
        return _parse_parallel(raw_step["parallel"], index=index)
    raise ConfigValidationError(
        f"pipeline step {index} must contain exactly one of folder or parallel"
    )


def _parse_parallel(raw_parallel: object, *, index: int) -> tuple[str, ...]:
    if not isinstance(raw_parallel, list):
        raise ConfigValidationError(f"pipeline step {index}.parallel must be a list")
    if len(raw_parallel) < 2:
        raise ConfigValidationError(
            f"pipeline step {index}.parallel must contain at least 2 folders"
        )
    folders: list[str] = []
    for parallel_index, raw_entry in enumerate(raw_parallel, start=1):
        if not isinstance(raw_entry, dict):
            raise ConfigValidationError(
                f"pipeline step {index}.parallel entry {parallel_index} must be a mapping"
            )
        _reject_unknown_keys(
            raw_entry,
            _ALLOWED_PARALLEL_ENTRY_KEYS,
            f"pipeline step {index}.parallel entry {parallel_index}",
        )
        if "folder" not in raw_entry:
            raise ConfigValidationError(
                f"pipeline step {index}.parallel entry {parallel_index} requires folder"
            )
        folders.append(
            _validate_folder_value(
                raw_entry["folder"],
                label=f"pipeline step {index}.parallel entry {parallel_index}.folder",
            )
        )
    return tuple(folders)


def _validate_folder_value(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"{label} must be a non-empty string")
    folder = value.strip()
    if "\\" in folder:
        raise ConfigValidationError(f"invalid folder path: {folder!r}")
    path = PurePosixPath(folder)
    if path.is_absolute():
        raise ConfigValidationError(f"invalid folder path: {folder!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ConfigValidationError(f"invalid folder path: {folder!r}")
    return path.as_posix()


def _validate_pipeline_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ConfigResolutionError("pipeline name is required")
    if not _PIPELINE_NAME.fullmatch(name):
        raise ConfigResolutionError(f"invalid pipeline name: {name!r}")
    if name == "all":
        raise ConfigResolutionError("pipeline name 'all' is reserved")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ConfigResolutionError(f"invalid pipeline name: {name!r}")
    return path.as_posix()


def _validate_pipeline_folders(root: Path, pipeline: Pipeline) -> None:
    from src.domain.config.folder_discovery import discover_folder_paths

    configured = discover_folder_paths(root)
    for step in pipeline.steps:
        for folder in step.folders:
            if folder not in configured:
                raise ConfigValidationError(f"unknown pipeline folder: {folder}")
