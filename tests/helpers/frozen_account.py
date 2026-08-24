# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frozen account binding fields for prepare-handler unit tests."""

from __future__ import annotations

import pytest

from src.domain.accounts.external_id import derive_external_id

HUB_ACCOUNT_ID = "111111111111"
TARGET_ACCOUNT_ID = "123456789012"
MAIN_ACCOUNT_ID = HUB_ACCOUNT_ID


def apply_prepare_handler_env(
    monkeypatch: pytest.MonkeyPatch, *, hub_account_id: str = HUB_ACCOUNT_ID
) -> None:
    monkeypatch.setenv("PROJECT_NAME", "openci-tf")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(
        "src.services.run_folder.prepare_and_submit.sts.get_caller_account_id",
        lambda credentials=None: (
            TARGET_ACCOUNT_ID if credentials is not None else hub_account_id
        ),
    )


def frozen_account_fields(
    *,
    account_id: str = TARGET_ACCOUNT_ID,
    hub_account_id: str = HUB_ACCOUNT_ID,
    readonly_role_name: str = "target",
    poweruser_role_name: str | None = None,
    max_ttl: int = 3600,
) -> dict[str, object]:
    external_id = derive_external_id(hub_account_id, account_id)
    binding = {
        "account_id": account_id,
        "readonly_role_name": readonly_role_name,
        "poweruser_role_name": poweruser_role_name,
        "external_id": external_id,
        "max_ttl": max_ttl,
    }
    return {"account_id": account_id, "account_binding": binding}
