# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve pinned repository configuration into the outer workflow contract."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.core.errors import ConfigResolutionError, ConfigValidationError
from src.core.models import GlobalSettings
from src.domain.cmd_builder.installers import installer_key, require_pinned_installer
from src.domain.config.folder_config import compact_folder_config_for_outer_state, parse_folder_config
from src.domain.config.folder_discovery import CONFIG_PATH, discover_folder_paths
from src.domain.config.folder_discovery import discover_folders as _discover_folders
from src.domain.config.global_config import parse_global_config
from src.domain.config.pipeline import load_pipeline

_CONFIG_PATH = CONFIG_PATH
_SHARED_INSTALLERS = (("tfsec", "1.28.10"), ("infracost", "0.10.39"))
_SAFE_ACTIONS = frozenset({"plan", "drift", "report", "plan_destroy", "apply", "destroy"})


def discover_folders(root: Path) -> list[str]:
    """Return canonical NFC folder keys for configured Terraform folders."""
    return _discover_folders(root)


def resolve_outer_state(
    clone_dir: str,
    folders: list[str],
    upstream_urls: object,
    action: str,
    *,
    pipeline: str | None = None,
) -> dict[str, Any]:
    """Return validated safe-lane inputs for explicit folders against a pinned clone."""
    if action not in _SAFE_ACTIONS:
        raise ConfigResolutionError(f"unsafe action: {action}")

    root = Path(clone_dir).resolve()
    path_map = discover_folder_paths(root)
    global_settings = _global_settings(root)
    if pipeline is not None:
        try:
            resolved_pipeline = load_pipeline(root, pipeline)
        except ConfigValidationError as error:
            raise ConfigResolutionError(f"invalid pipeline {pipeline!r}: {error}") from error
        steps = _pipeline_steps(resolved_pipeline.steps, action=action)
        folders = [folder for step in steps for folder in step]
    elif folders:
        steps = [list(folders)]
    else:
        raise ConfigResolutionError("no configured folders found")

    missing = [folder for folder in folders if folder not in path_map]
    if missing:
        raise ConfigResolutionError(f"unknown folder: {', '.join(missing)}")
    configs = {
        folder: _read_folder_config(root, path_map[folder], global_settings)
        for folder in folders
    }
    return {
        "folder_configs": configs,
        "upstream_urls": _validated_upstream_urls(upstream_urls, configs, action),
        "folders": folders,
        "steps": steps,
    }


def _pipeline_steps(raw_steps: tuple[Any, ...], *, action: str) -> list[list[str]]:
    steps = [list(step.folders) for step in raw_steps]
    if action == "plan_destroy":
        steps.reverse()
    return steps


def _global_settings(root: Path) -> GlobalSettings | None:
    path = root / _CONFIG_PATH
    if not path.is_file():
        return None
    try:
        return parse_global_config(path.read_text()).settings
    except (OSError, ConfigValidationError) as error:
        raise ConfigResolutionError("repository global configuration is invalid") from error


def _read_folder_config(root: Path, physical_folder: str, global_settings: GlobalSettings | None) -> dict[str, Any]:
    candidate = (root / physical_folder).resolve()
    if root not in candidate.parents or candidate == root:
        raise ConfigResolutionError(f"unknown folder: {physical_folder}")
    if not candidate.is_dir():
        raise ConfigResolutionError(f"unknown folder: {physical_folder}")
    path = candidate / _CONFIG_PATH
    if not path.is_file():
        raise ConfigResolutionError(f"missing configuration for folder: {physical_folder}")
    try:
        config = parse_folder_config(path.read_text(), global_settings)
    except OSError as error:
        raise ConfigResolutionError(f"invalid configuration for folder: {physical_folder}") from error
    except ConfigValidationError as error:
        raise ConfigResolutionError(f"invalid configuration for folder: {physical_folder}: {error}") from error
    return compact_folder_config_for_outer_state(asdict(config))


def _validated_upstream_urls(raw_urls: object, configs: dict[str, dict[str, Any]], action: str) -> dict[str, str]:
    if not isinstance(raw_urls, dict):
        raise ConfigResolutionError("upstream_urls settings must be an object")
    required = {tuple(str(config["tf_runtime"]).split(":", 1)) for config in configs.values()}
    if action in {"plan", "report", "plan_destroy"}:
        required |= set(_SHARED_INSTALLERS)
    binary_counts: dict[str, int] = {}
    for binary, _version in required:
        binary_counts[binary] = binary_counts.get(binary, 0) + 1
    urls: dict[str, str] = {}
    for binary, version in sorted(required, key=lambda item: installer_key(*item)):
        try:
            require_pinned_installer(binary, version)
        except ValueError as error:
            raise ConfigResolutionError(str(error)) from error
        key = installer_key(binary, version)
        url = raw_urls.get(key)
        if url is None and binary_counts[binary] == 1:
            url = raw_urls.get(binary)
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ConfigResolutionError(f"upstream_urls missing a valid URL for pinned installer {key}")
        urls[key] = url
    return urls
