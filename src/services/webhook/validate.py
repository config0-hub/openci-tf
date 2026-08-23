"""HMAC-SHA256 webhook signature validation."""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(payload_body: bytes, signature: str, secret: str) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature.

    Args:
        payload_body: Raw request body bytes.
        signature: The X-Hub-Signature-256 header value (e.g. "sha256=abc...").
        secret: The shared HMAC secret.

    Returns:
        True if valid, False otherwise.
    """
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
