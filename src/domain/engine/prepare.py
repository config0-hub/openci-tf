"""Testable atomic package preparation and engine submission sequence."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.domain.engine.byte_budget import check_payload_size


def prepare_and_submit(
    *,
    payload: dict[str, Any],
    secrets: dict[str, str],
    encrypt: Callable[[str], str],
    package: Callable[[str], str],
    upload: Callable[[str], None],
    submit: Callable[[dict[str, Any]], object],
    pre_submit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Encrypt and upload before the final checked engine-submission boundary."""
    check_payload_size(json.dumps(payload, separators=(",", ":")).encode())
    fd, plain = tempfile.mkstemp(prefix="openci-tf-secrets-", suffix=".json")
    try:
        with open(fd, "w", closefd=True) as handle:
            json.dump(secrets, handle)
        encrypted = encrypt(plain)
        archive = package(encrypted)  # package must write secrets.enc.json
        upload(archive)
        if pre_submit is not None:
            pre_submit()
        submitted_at = time.time()
        ack = submit(payload)
        result: dict[str, Any] = {"payload": payload, "submitted_at": submitted_at}
        if isinstance(ack, dict):
            for key in ("engine_execution_arn", "codebuild_build_id"):
                if key in ack:
                    result[key] = ack[key]
        return result
    finally:
        Path(plain).unlink(missing_ok=True)
