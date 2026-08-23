"""Dedicated SSM namespace for Infracost API keys."""

from __future__ import annotations

import re

INFRACOST_KEY_PREFIX = "/openci-tf/infracost/"
_LEAF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_infracost_key_path(path: str) -> str:
    """Reject paths outside the reserved Infracost key namespace."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("ssm_infracost_api_key is required")
    normalized = path.strip()
    if not normalized.startswith(INFRACOST_KEY_PREFIX):
        raise ValueError(f"ssm_infracost_api_key must be under {INFRACOST_KEY_PREFIX}")
    segments = [segment for segment in normalized.split("/") if segment]
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("invalid infracost key path")
    if any(ord(char) < 33 for segment in segments for char in segment):
        raise ValueError("invalid infracost key path")
    leaf = normalized.removeprefix(INFRACOST_KEY_PREFIX)
    if not leaf or not _LEAF_NAME.fullmatch(leaf):
        raise ValueError("invalid infracost key path")
    return normalized
