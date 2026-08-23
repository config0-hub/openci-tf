# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for engine done-marker error derivation."""
from src.domain.engine.result import derive_error_from_steps, parse_result


def test_derive_error_ignores_curl_progress_meter():
    output = "100  6068   0     0 100  6068     0 60720  --:--:-- --:--:-- --:--:-- 61292"
    derived = derive_error_from_steps([{"status": "failed", "output": output, "exit_code": 1}])
    assert derived == "step failed with exit code 1"


def test_derive_error_prefers_final_access_denied_line():
    output = (
        "Refreshing state...\n"
        "Error: reading IAM Role (probe): operation error IAM: GetRole,\n"
        "https response error StatusCode: 403, RequestID: abc-123,\n"
        "api error AccessDenied: User is not authorized to perform: iam:GetRole\n"
        "with an explicit deny in an identity-based policy"
    )
    derived = derive_error_from_steps([{"status": "failed", "output": output}])
    assert derived is not None
    assert "AccessDenied" in derived
    assert "iam:GetRole" in derived


def test_derive_error_sanitizes_secret_like_values():
    output = "Error: failed with secret=super-secret-token-value"
    derived = derive_error_from_steps([{"status": "failed", "output": output}])
    assert derived == "Error: failed with secret=***"


def test_derive_error_is_length_capped():
    output = "Error: " + ("x" * 700)
    derived = derive_error_from_steps([{"status": "failed", "output": output}])
    assert derived is not None
    assert len(derived) == 256
    assert derived.endswith("...")


def test_parse_result_derives_error_when_top_level_error_missing():
    marker = {
        "trigger_id": "run",
        "status": "failed",
        "steps": [
            {
                "step_name": "step-0",
                "status": "failed",
                "exit_code": 1,
                "duration_seconds": 1.0,
                "output": "Error: tofu plan failed",
            }
        ],
    }
    result = parse_result(marker, "run")
    assert result.error == "Error: tofu plan failed"


def test_parse_result_preserves_explicit_top_level_error():
    marker = {
        "trigger_id": "run",
        "status": "failed",
        "steps": [
            {
                "step_name": "step-0",
                "status": "failed",
                "exit_code": 1,
                "duration_seconds": 1.0,
                "output": "Error: step output only",
            }
        ],
        "error": "engine reported failure",
    }
    result = parse_result(marker, "run")
    assert result.error == "engine reported failure"
