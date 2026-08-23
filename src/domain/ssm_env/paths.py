# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate SSM parameter paths for folder execution environment."""

from __future__ import annotations

import re

from src.core.errors import ConfigValidationError
from src.domain.engine.artifact_limits import MAX_SSM_ENV_PATH_CHARS

SSM_ENV_PREFIX = "/openci-tf/env/"
_AWS_MAX_PARAMETER_NAME_LEN = 2048
_MAX_SSM_ENV_PATHS = 4
_PATH_BODY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def validate_ssm_env_path(path: str) -> str:
    """Return a normalized absolute SSM path under ``/openci-tf/env/``."""
    if not isinstance(path, str) or not path:
        raise ConfigValidationError("ssm_env_paths entries must be non-empty strings")
    if path != path.strip() or any(char.isspace() for char in path):
        raise ConfigValidationError("ssm_env_paths entry is malformed")
    if not path.startswith(SSM_ENV_PREFIX):
        raise ConfigValidationError(f"ssm_env_paths entries must begin with {SSM_ENV_PREFIX}")
    if path.endswith("/") or "//" in path:
        raise ConfigValidationError("ssm_env_paths entry is malformed")
    suffix = path.removeprefix(SSM_ENV_PREFIX)
    if not suffix:
        raise ConfigValidationError("ssm_env_paths entry has an empty suffix")
    if "*" in path or "?" in path:
        raise ConfigValidationError("ssm_env_paths entries must not contain wildcards")
    segments = path.split("/")
    if segments[0] != "":
        raise ConfigValidationError("ssm_env_paths entry is malformed")
    for segment in segments[1:]:
        if not segment or segment in {".", ".."}:
            raise ConfigValidationError("ssm_env_paths entry is malformed")
        if any(ord(char) < 33 for char in segment):
            raise ConfigValidationError("ssm_env_paths entry is malformed")
    if not _PATH_BODY.fullmatch(suffix):
        raise ConfigValidationError("ssm_env_paths entry is malformed")
    if len(path) > MAX_SSM_ENV_PATH_CHARS:
        raise ConfigValidationError(f"ssm_env_paths entry exceeds {MAX_SSM_ENV_PATH_CHARS} characters")
    if len(path) > _AWS_MAX_PARAMETER_NAME_LEN:
        raise ConfigValidationError("ssm_env_paths entry exceeds AWS parameter name length")
    return path


def validate_ssm_env_paths(raw: object) -> tuple[str, ...]:
    """Parse folder-config ``ssm_env_paths`` as a non-empty list when present."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise ConfigValidationError("ssm_env_paths must be a list, not a scalar string")
    if not isinstance(raw, list):
        raise ConfigValidationError("ssm_env_paths must be a list")
    if len(raw) > _MAX_SSM_ENV_PATHS:
        raise ConfigValidationError(f"ssm_env_paths exceeds limit of {_MAX_SSM_ENV_PATHS}")
    seen: set[str] = set()
    normalized: list[str] = []
    for item in raw:
        path = validate_ssm_env_path(item)
        if path in seen:
            raise ConfigValidationError(f"duplicate ssm_env_paths entry: {path}")
        seen.add(path)
        normalized.append(path)
    return tuple(normalized)
