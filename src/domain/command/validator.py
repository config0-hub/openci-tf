# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validate a parsed v3 Command against configured folders."""

from __future__ import annotations

from src.core.models import Command, FolderConfig, GlobalConfig


class ValidationError(Exception):
    """Raised when a command fails validation."""


def validate_command(
    command: Command,
    global_config: GlobalConfig,
    folder_configs: dict[str, FolderConfig] | None = None,
) -> None:
    """Validate folder references after v3 command parsing.

    ``all`` is resolved to concrete folders by the caller, so it needs no
    per-folder checks here.  Apply and destroy are represented by Command but
    fail in the resolver before this legacy validation path can dispatch them.
    """
    del global_config
    if command.all_flag:
        return
    if not command.folders:
        raise ValidationError(f"{command.action} requires at least one folder")
    configs = folder_configs or {}
    for folder in command.folders:
        if folder not in configs:
            raise ValidationError(f"Folder {folder!r} has no .openci_tf/config.yaml")
