# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable execution identifiers with attempt uniqueness."""

import hashlib

from src.domain.engine.invocation_id import assert_execution_id_bounds


def compose_execution_id(outer_run_id: str, folder: str, attempt: int) -> str:
    if not outer_run_id or not folder or attempt < 0:
        raise ValueError("invalid execution id inputs")
    digest = hashlib.sha256(folder.encode()).hexdigest()[:12]
    execution_id = f"{outer_run_id}.{digest}.{attempt}"
    assert_execution_id_bounds(execution_id)
    return execution_id
