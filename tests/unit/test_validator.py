# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for v3 folder validation."""

import pytest

from src.core.models import Command, FolderConfig, GlobalConfig
from src.domain.command.validator import ValidationError, validate_command


def test_known_folder_is_valid() -> None:
    validate_command(
        Command(action="plan", folders=["infra/vpc"]),
        GlobalConfig(), {"infra/vpc": FolderConfig(account_alias="main")},
    )


def test_unknown_folder_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no .openci_tf/config.yaml"):
        validate_command(Command(action="plan", folders=["missing"]), GlobalConfig(), {})


def test_all_is_validated_after_folder_discovery() -> None:
    validate_command(Command(action="plan", all_flag=True), GlobalConfig())
