# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S3 lifecycle-aligned expiry helpers for manifests and API reads."""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone


def _iso_z(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from error
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def tmp_retention_days() -> int:
    return _positive_int("TMP_LIFECYCLE_DAYS", 3)


def package_retention_days() -> int:
    return _positive_int("PACKAGE_LIFECYCLE_DAYS", 30)


def done_retention_days() -> int:
    return _positive_int("DONE_LIFECYCLE_DAYS", 365)


def s3_lifecycle_expiration_utc(last_modified: datetime, retention_days: int) -> datetime:
    """Midnight UTC on the S3 lifecycle expiration day for a day-based rule."""
    base = last_modified.astimezone(timezone.utc)
    expiry_date = base.date() + timedelta(days=retention_days)
    return datetime.combine(expiry_date, time(0, 0), tzinfo=timezone.utc)


def conservative_api_expiry_iso(last_modified: datetime, retention_days: int) -> str:
    """Conservative API artifact expiry derived from object LastModified and retention days."""
    return _iso_z(s3_lifecycle_expiration_utc(last_modified, retention_days))
