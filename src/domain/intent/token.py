# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cryptographically random intent confirmation tokens."""
from __future__ import annotations

import secrets


def mint_token(*, nbytes: int = 4) -> str:
    """Return a 6-8 character lowercase hex token."""
    if nbytes not in {3, 4}:
        raise ValueError("token length must be 6 or 8 hex characters")
    return secrets.token_hex(nbytes)


def mint_intent_id() -> str:
    """Return a non-secret intent identifier, distinct from the confirm token."""
    return f"intent-{secrets.token_hex(8)}"
