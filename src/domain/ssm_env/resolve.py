"""Fetch and merge hub SSM dotenv parameters into execution secrets."""

from __future__ import annotations

from collections.abc import Callable

from src.core.errors import SsmEnvError
from src.domain.ssm_env.dotenv import (
    _MAX_MERGED_ENV_VARS,
    _MAX_MERGED_TOTAL_VALUE_BYTES,
    parse_dotenv,
)


def resolve_ssm_env_vars(
    paths: tuple[str, ...],
    *,
    fetch: Callable[[str], str],
    existing: dict[str, str],
) -> dict[str, str]:
    """Fetch, parse, and merge dotenv parameters without overwriting existing keys."""
    merged: dict[str, str] = {}
    total_value_bytes = 0
    for path in paths:
        raw = fetch(path)
        if not isinstance(raw, str):
            raise SsmEnvError(f"SSM parameter {path} returned non-text content")
        parsed = parse_dotenv(raw, source=path)
        for key, value in parsed.items():
            if key in merged:
                raise SsmEnvError(f"duplicate environment variable {key} across SSM parameters")
            if key in existing:
                raise SsmEnvError(f"environment variable {key} from {path} collides with existing secrets")
            merged[key] = value
            total_value_bytes += len(value.encode("utf-8"))
            if len(merged) > _MAX_MERGED_ENV_VARS:
                raise SsmEnvError(f"merged SSM env exceeds {_MAX_MERGED_ENV_VARS} variables")
            if total_value_bytes > _MAX_MERGED_TOTAL_VALUE_BYTES:
                raise SsmEnvError(
                    f"merged SSM env exceeds {_MAX_MERGED_TOTAL_VALUE_BYTES} bytes of decoded values"
                )
    return merged
