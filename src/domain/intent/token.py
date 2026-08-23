"""Cryptographically random intent confirmation tokens."""
from __future__ import annotations

import secrets


def mint_token(*, nbytes: int = 4) -> str:
    """Return a 6-8 character lowercase hex token."""
    if nbytes not in {3, 4}:
        raise ValueError("token length must be 6 or 8 hex characters")
    return secrets.token_hex(nbytes)
