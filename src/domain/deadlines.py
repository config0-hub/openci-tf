# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One absolute UTC deadline shared by every run consumer."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Iterable

from src.core.errors import DeadlineExceededError

UTC_DEADLINE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_deadline(epoch_seconds: int) -> str:
    """Format an epoch second as canonical UTC RFC3339."""
    if not isinstance(epoch_seconds, int) or isinstance(epoch_seconds, bool):
        raise TypeError("deadline epoch must be an integer")
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime(
        UTC_DEADLINE_FORMAT
    )


def deadline_epoch(deadline_at: str) -> int:
    """Parse the canonical UTC deadline without accepting ambiguous timestamps."""
    if not isinstance(deadline_at, str):
        raise TypeError("deadline_at must be a UTC RFC3339 string")
    try:
        parsed = datetime.strptime(deadline_at, UTC_DEADLINE_FORMAT)
    except ValueError as error:
        raise ValueError("deadline_at must use YYYY-MM-DDTHH:MM:SSZ") from error
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


def compute_deadline_at(
    action: str,
    folder_windows: Iterable[tuple[int, int]],
    *,
    resolved_at: int,
) -> str:
    """Compute the run deadline once from validated folder budget/grace windows.

    Read folders execute in parallel, so the longest folder controls the run window.
    Apply and destroy execute serially and wait each folder's grace period, so every
    folder budget and grace period is included in deterministic order.
    """
    windows = tuple(folder_windows)
    if not windows:
        raise ValueError("at least one folder window is required")
    for budget, grace in windows:
        if budget <= 0:
            raise ValueError("folder budget must be positive")
        if grace < 0:
            raise ValueError("folder grace must be non-negative")
    if action in {"plan", "drift", "report", "plan_destroy"}:
        duration = max(budget for budget, _grace in windows)
    elif action in {"apply", "destroy"}:
        duration = sum(budget + grace for budget, grace in windows)
    else:
        raise ValueError(f"unknown action: {action}")
    return format_deadline(resolved_at + duration)


def remaining_seconds(
    deadline_at: str,
    *,
    now: float | None = None,
    cap_seconds: int | None = None,
) -> int:
    """Return a positive, non-extending budget bounded by ``deadline_at``."""
    current = time.time() if now is None else now
    remaining = math.ceil(deadline_epoch(deadline_at) - current)
    if remaining <= 0:
        raise DeadlineExceededError("run deadline has expired")
    if cap_seconds is not None:
        if cap_seconds <= 0:
            raise ValueError("deadline cap must be positive")
        return min(remaining, cap_seconds)
    return remaining
