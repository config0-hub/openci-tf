# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure folder-path normalization helpers shared across layers."""

from __future__ import annotations

import hashlib
import unicodedata

# The byte cap is part of this function's contract; domain policy in
# src/domain/run/limits.py binds to this value rather than restating it.
MAX_FOLDER_PATH_UTF8_BYTES = 192


def normalize_folder_path(folder: str) -> str:
    normalized = unicodedata.normalize("NFC", folder.strip())
    if not normalized:
        raise ValueError("invalid folder path")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in normalized):
        raise ValueError("folder path contains unsupported control characters")
    if len(normalized.encode("utf-8")) > MAX_FOLDER_PATH_UTF8_BYTES:
        raise ValueError("folder path exceeds maximum UTF-8 byte length")
    return normalized


def folder_opaque_key(folder: str) -> str:
    return hashlib.sha256(normalize_folder_path(folder).encode("utf-8")).hexdigest()
