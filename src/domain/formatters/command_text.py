# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared command-line redaction, normalization, and bounding."""

from __future__ import annotations

import hashlib
import re

MAX_COMMAND_CONTEXT_CHARS = 200
_TRUNCATION_HASH_CHARS = 12
_CONFIRM_TOKEN_RE = re.compile(r"\b(confirm)\s+(\S+)", re.IGNORECASE)
_HTML_COMMENT_BLOCK_RE = re.compile(r"<!--.*?-->")
_COMMENT_OBJECT_ID_TOKEN_RE = re.compile(r"\bcomment_object_id\s*:", re.IGNORECASE)


def redact_confirm_token(text: str) -> str:
    """Redact one-time confirm tokens from user-provided command text."""
    return _CONFIRM_TOKEN_RE.sub(r"\1 <redacted>", text)


def sanitize_command_line(command_text: str) -> str:
    """Collapse whitespace, strip comment markers, and escape Markdown table pipes."""
    without_html_comments = _HTML_COMMENT_BLOCK_RE.sub(" ", command_text)
    without_html_markers = without_html_comments.replace("<!--", " ").replace("-->", " ")
    neutralized_markers = _COMMENT_OBJECT_ID_TOKEN_RE.sub(
        "comment_object_id_", without_html_markers
    )
    collapsed = " ".join(neutralized_markers.split()).replace("`", "")
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
    """Return the redacted, whitespace-collapsed, bounded command line."""
    return bound_command_line(sanitize_command_line(redact_confirm_token(command_text)))
