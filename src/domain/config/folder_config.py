# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict per-folder configuration parsing."""

import json
import re
from typing import Any

import yaml

from src.core.errors import ConfigValidationError
from src.core.models import (
    DEFAULT_APPLY_GRACE_SECONDS,
    DEFAULT_DESTROY_GRACE_SECONDS,
    FolderConfig,
    GlobalSettings,
    MAX_MUTATION_GRACE_SECONDS,
    MutationVerbConfig,
)
from src.domain.cmd_builder.installers import require_pinned_runtime
from src.domain.engine.artifact_limits import (
    MAX_ACCOUNT_ALIAS_CHARS,
    MAX_EXTRA_FLAG_CHARS,
    MAX_EXTRA_FLAGS_COUNT,
    MAX_EXTRA_FLAGS_SERIALIZED_BYTES,
)
from src.domain.ssm_env.paths import validate_ssm_env_paths

_RUNTIME = re.compile(r"^(tofu|terraform):\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_TARGETS = {"lambda", "codebuild"}


def _validate_extra_flags(extra_flags: object) -> tuple[str, ...]:
    if not isinstance(extra_flags, list) or not all(
        isinstance(flag, str) for flag in extra_flags
    ):
        raise ConfigValidationError("extra_flags must be a list of strings")
    if len(extra_flags) > MAX_EXTRA_FLAGS_COUNT:
        raise ConfigValidationError(
            f"extra_flags exceeds limit of {MAX_EXTRA_FLAGS_COUNT}"
        )
    normalized: list[str] = []
    for flag in extra_flags:
        if len(flag) > MAX_EXTRA_FLAG_CHARS:
            raise ConfigValidationError(
                f"extra_flags entry exceeds {MAX_EXTRA_FLAG_CHARS} characters"
            )
        normalized.append(flag)
    serialized = json.dumps(normalized, separators=(",", ":")).encode()
    if len(serialized) > MAX_EXTRA_FLAGS_SERIALIZED_BYTES:
        raise ConfigValidationError(
            f"extra_flags serialized size exceeds {MAX_EXTRA_FLAGS_SERIALIZED_BYTES} bytes"
        )
    return tuple(normalized)


def _validate(
    runtime: str, target: str, timeout: int, alias: object, extra_flags: object
) -> tuple[str, tuple[str, ...]]:
    if not _RUNTIME.fullmatch(runtime):
        raise ConfigValidationError(
            "tf_runtime must use an allowlisted binary and SemVer"
        )
    try:
        require_pinned_runtime(runtime)
    except ValueError as error:
        raise ConfigValidationError(str(error)) from error
    if target not in _TARGETS:
        raise ConfigValidationError("execution_target is not allowed")
    if not isinstance(timeout, int) or not 60 <= timeout <= 3600:
        raise ConfigValidationError("timeout must be between 60 and 3600")
    if not isinstance(alias, str) or not alias.strip():
        raise ConfigValidationError("account_alias is required")
    if len(alias) > MAX_ACCOUNT_ALIAS_CHARS:
        raise ConfigValidationError(
            f"account_alias exceeds {MAX_ACCOUNT_ALIAS_CHARS} characters"
        )
    validated_flags = _validate_extra_flags(extra_flags)
    return alias, validated_flags


def _parse_grace_seconds(value: object, *, verb: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(f"{verb}.grace_seconds must be an integer")
    if not 0 <= value <= MAX_MUTATION_GRACE_SECONDS:
        raise ConfigValidationError(
            f"{verb}.grace_seconds must be between 0 and {MAX_MUTATION_GRACE_SECONDS}"
        )
    return value


_MUTATION_VERB_KEYS = frozenset({"allow", "grace_seconds"})


def compact_folder_config_for_outer_state(config: dict[str, Any]) -> dict[str, Any]:
    """Strip disabled mutation blocks while retaining enabled grace periods."""
    compact = dict(config)
    for verb, default_grace in (
        ("apply", DEFAULT_APPLY_GRACE_SECONDS),
        ("destroy", DEFAULT_DESTROY_GRACE_SECONDS),
    ):
        block = compact.get(verb)
        if not isinstance(block, dict) or block.get("allow") is not True:
            compact.pop(verb, None)
            continue
        compact[verb] = {
            "allow": True,
            "grace_seconds": block.get("grace_seconds", default_grace),
        }
    return compact


def expand_folder_config_from_outer_state(config: dict[str, Any]) -> dict[str, Any]:
    """Restore omitted default mutation blocks when rehydrating inner map items."""
    expanded = dict(config)
    for verb, default_grace in (
        ("apply", DEFAULT_APPLY_GRACE_SECONDS),
        ("destroy", DEFAULT_DESTROY_GRACE_SECONDS),
    ):
        block = expanded.get(verb)
        if block is None:
            expanded[verb] = {"allow": False, "grace_seconds": default_grace}
        elif isinstance(block, dict):
            expanded[verb] = {
                "allow": bool(block.get("allow")),
                "grace_seconds": block.get("grace_seconds", default_grace),
            }
    return expanded


def _parse_mutation_block(
    raw: object,
    *,
    verb: str,
    default_grace: int,
) -> MutationVerbConfig:
    """Parse apply/destroy mapping blocks (mapping-only; booleans are rejected)."""
    if raw is None:
        return MutationVerbConfig(allow=False, grace_seconds=default_grace)
    if isinstance(raw, bool):
        raise ConfigValidationError(
            f"{verb} must be a mapping with allow/grace_seconds; boolean shorthand is not supported"
        )
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            f"{verb} must be a mapping with allow/grace_seconds"
        )
    unknown = sorted(set(raw) - _MUTATION_VERB_KEYS)
    if unknown:
        raise ConfigValidationError(f"{verb} has unknown keys: {', '.join(unknown)}")
    if "allow" not in raw:
        raise ConfigValidationError(
            f"{verb}.allow is required when {verb} is a mapping"
        )
    allow = raw.get("allow")
    if not isinstance(allow, bool):
        raise ConfigValidationError(f"{verb}.allow must be a boolean")
    grace = _parse_grace_seconds(
        raw.get("grace_seconds"), verb=verb, default=default_grace
    )
    return MutationVerbConfig(allow=allow, grace_seconds=grace)


def parse_folder_config(
    yaml_content: str, global_settings: GlobalSettings | None = None
) -> FolderConfig:
    data = yaml.safe_load(yaml_content) or {}
    if not isinstance(data, dict):
        raise ConfigValidationError("folder config must be a mapping")
    defaults = global_settings or GlobalSettings()
    runtime, timeout = (
        data.get("tf_runtime", defaults.tf_runtime),
        data.get("timeout", defaults.default_timeout),
    )
    target, alias, extra_flags = (
        data.get("execution_target", "lambda"),
        data.get("account_alias"),
        data.get("extra_flags", []),
    )
    ssm_env_paths = validate_ssm_env_paths(data.get("ssm_env_paths"))
    account_alias, validated_flags = _validate(
        runtime, target, timeout, alias, extra_flags
    )
    apply_config = _parse_mutation_block(
        data.get("apply"),
        verb="apply",
        default_grace=DEFAULT_APPLY_GRACE_SECONDS,
    )
    destroy_config = _parse_mutation_block(
        data.get("destroy"),
        verb="destroy",
        default_grace=DEFAULT_DESTROY_GRACE_SECONDS,
    )
    return FolderConfig(
        version=data.get("version", 1),
        timeout=timeout,
        tf_runtime=runtime,
        account_alias=account_alias,
        execution_target=target,
        extra_flags=validated_flags,
        ssm_env_paths=ssm_env_paths,
        apply=apply_config,
        destroy=destroy_config,
    )
