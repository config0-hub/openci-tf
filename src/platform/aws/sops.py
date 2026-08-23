# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SOPS encryption without leaking target-role credentials to subprocesses."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_LAMBDA_IDENTITY_KEYS = ("PATH", "HOME", "LANG", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION")


def encrypt_file(plaintext_path: str, kms_key_arn: str, runner=subprocess.run) -> str:
    output = f"{plaintext_path}.enc"
    env = {key: os.environ[key] for key in _LAMBDA_IDENTITY_KEYS if key in os.environ}
    env["SOPS_KMS_ARN"] = kms_key_arn
    try:
        result = runner(["sops", "--encrypt", "--output", output, plaintext_path], env=env, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"sops encryption failed: {result.stderr}")
        return output
    finally:
        with open(plaintext_path, "r+b") as handle:
            handle.write(b"\0" * os.path.getsize(plaintext_path))
        Path(plaintext_path).unlink(missing_ok=True)
