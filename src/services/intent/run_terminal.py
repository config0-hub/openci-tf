# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Terminalize intent-creation runs without pipeline-apply indexing."""

from __future__ import annotations

import os
from typing import Any

from src.platform.aws.run_registry import update_run_status


def terminalize_intent_create_run(event: dict[str, Any], status: str) -> None:
    """Mark one intent-creation run terminal without indexing a pipeline apply step."""
    if status not in {"succeeded", "failed"}:
        raise ValueError(f"unsupported intent-create terminal status: {status}")
    if not os.environ.get("RUN_REGISTRY_TABLE_NAME"):
        return
    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("intent-create run terminalization requires run_id")
    update_run_status(run_id, status)
