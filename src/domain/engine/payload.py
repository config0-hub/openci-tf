# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validated v3 SimplePayload representation."""

import base64
import binascii
from dataclasses import dataclass


@dataclass(frozen=True)
class EnginePayload:
    trigger_id: str
    s3_package_uri: str
    sops_type: str | None
    sops_path: str | None
    commands_b64: str
    done_endpoint: str
    execution_target: str
    timeout_seconds: int

    def validate(self) -> None:
        required = (self.trigger_id, self.s3_package_uri, self.commands_b64, self.done_endpoint, self.execution_target)
        if any(not isinstance(value, str) or not value for value in required):
            raise ValueError("required SimplePayload fields must be non-empty strings")
        if self.sops_type not in {"ssm", "kms", None}:
            raise ValueError("sops_type must be ssm, kms, or None")
        if self.sops_type == "ssm" and (not isinstance(self.sops_path, str) or not self.sops_path):
            raise ValueError("sops_path is required for ssm")
        if not self.s3_package_uri.startswith("s3://") or not self.done_endpoint.startswith("s3://"):
            raise ValueError("package and done endpoints must be s3 URIs")
        if self.execution_target not in {"lambda", "codebuild"}:
            raise ValueError("unknown execution target")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        try:
            commands = __import__("json").loads(base64.b64decode(self.commands_b64, validate=True))
            if not isinstance(commands, list) or not commands or any(not isinstance(item, str) or not item.strip() for item in commands):
                raise ValueError("commands_b64 must decode to a non-empty JSON command array")
        except (binascii.Error, ValueError, TypeError) as error:
            raise ValueError("commands_b64 must be base64 JSON") from error
