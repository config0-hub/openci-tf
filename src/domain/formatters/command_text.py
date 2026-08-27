# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared command-line redaction, normalization, and bounding."""

from __future__ import annotations

import hashlib
import re

MAX_COMMAND_CONTEXT_CHARS = 200
_TRUNCATION_HASH_CHARS = 12
_CONFIRM_TOKEN_RE = re.compile(r"\b(confirm)\s+(\S+)", re.IGNORECASE)


def redact_confirm_token(text: str) -> str:
    """Redact one-time confirm tokens from user-provided command text."""
    return _CONFIRM_TOKEN_RE.sub(r"\1 <redacted>", text)


def sanitize_command_line(command_text: str) -> str:
    """Collapse whitespace, drop backticks, and escape pipes for Markdown table cells."""
    collapsed = " ".join(command_text.split()).replace("`", "")
    unescaped = collapsed.replace("\\|", "|")
    return unescaped.replace("|", "\\|")


def bound_command_line(command_text: str) -> str:
    """Cap one normalized command line, suffixing a sha256 prefix when cut."""
    if len(command_text) <= MAX_COMMAND_CONTEXT_CHARS:
        return command_text
    digest = hashlib.sha256(command_text.encode("utf-8")).hexdigest()[
        :_TRUNCATION_HASH_CHARS
    ]
    return f"{command_text[:MAX_COMMAND_CONTEXT_CHARS]} [truncated sha256:{digest}]"


def normalized_command_context_line(command_text: str) -> str:
    """Return the single redacted, whitespace-collapsed, bounded command line."""
    first_line = (
        command_text.strip().splitlines()[0].strip() if command_text.strip() else ""
    )
    return bound_command_line(sanitize_command_line(redact_confirm_token(first_line)))
