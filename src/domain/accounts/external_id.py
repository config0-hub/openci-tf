"""Canonical target-role ExternalId derivation for openci-tf.

The value is implementation-owned, not a secret, and is derived identically by
hub registration/runtime and target Terraform:

    openci-tf- + first 16 lowercase hex chars of SHA-256(
        UTF-8 "openci-tf:<hub-account-id>:<target-account-id>"
    )
"""
from __future__ import annotations

import hashlib
import re
import sys

_ACCOUNT_ID = re.compile(r"^\d{12}$")
_PREFIX = "openci-tf"
_HEX_CHARS = 16


def _validate_account_id(name: str, account_id: str) -> None:
    if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError(f"{name} must be exactly 12 decimal digits")


def derive_external_id(hub_account_id: str, target_account_id: str) -> str:
    """Return the canonical target-role ExternalId for a hub/target pair."""
    _validate_account_id("hub_account_id", hub_account_id)
    _validate_account_id("target_account_id", target_account_id)
    digest = hashlib.sha256(f"{_PREFIX}:{hub_account_id}:{target_account_id}".encode()).hexdigest()
    return f"{_PREFIX}-{digest[:_HEX_CHARS]}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("Usage: derive_external_id.sh <12-digit-hub-account-id> <12-digit-target-account-id>", file=sys.stderr)
        return 1
    try:
        print(derive_external_id(args[0], args[1]))
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
