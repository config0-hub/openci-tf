# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedicated SSM namespace for repository clone tokens."""

from __future__ import annotations

import re

CLONE_TOKEN_PREFIX = "/openci-tf/clone-token/"
_LEAF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_clone_token_path(path: str) -> str:
    """Reject paths outside the clone-token namespace."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("ssm_openci_tf_github_token is required")
    normalized = path.strip()
    if not normalized.startswith(CLONE_TOKEN_PREFIX):
        raise ValueError(f"ssm_openci_tf_github_token must be under {CLONE_TOKEN_PREFIX}")
    segments = [segment for segment in normalized.split("/") if segment]
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("invalid clone token path")
    if any(ord(char) < 33 for segment in segments for char in segment):
        raise ValueError("invalid clone token path")
    leaf = normalized.removeprefix(CLONE_TOKEN_PREFIX)
    if not leaf or not _LEAF_NAME.fullmatch(leaf):
        raise ValueError("invalid clone token path")
    return normalized
