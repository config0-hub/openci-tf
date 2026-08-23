# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authorization policy independent of the GitHub transport."""
def can_trigger(permission: str) -> bool:
    return permission.lower() in {"write", "admin"}
