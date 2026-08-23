# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Domain policy limits for run requests and folder paths."""

from __future__ import annotations

from src.core.registry_schema import MAX_FOLDER_PATH_UTF8_BYTES

MAX_FOLDERS_PER_REQUEST = 50
MAX_FOLDER_PATH_LENGTH = MAX_FOLDER_PATH_UTF8_BYTES
MAX_REQUEST_BODY_BYTES = 32_768
