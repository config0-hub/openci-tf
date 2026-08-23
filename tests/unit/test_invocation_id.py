# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Invocation identity and run_id derivation tests."""

from __future__ import annotations

import re

import pytest

from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.invocation_id import (
    InvalidInvocationIdentityError,
    derive_run_id,
    extract_delivery_id,
    extract_request_id,
    validate_delivery_id,
)

_GUID_A = "38355582-3487-2086-500a-1b2c3d4e5f60"
_GUID_B = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def test_extract_delivery_id_is_case_insensitive():
    assert extract_delivery_id({"x-github-delivery": _GUID_A}) == _GUID_A.lower()
    assert extract_delivery_id({"X-GitHub-Delivery": _GUID_B.upper()}) == _GUID_B.lower()


def test_extract_delivery_id_missing_returns_none():
    assert extract_delivery_id({}) is None


@pytest.mark.parametrize("value", ["", "abc", "12-34", "not-a-uuid", "1234567890"])
def test_extract_delivery_id_rejects_bad_shape(value):
    with pytest.raises(InvalidInvocationIdentityError):
        extract_delivery_id({"x-github-delivery": value})


def test_extract_request_id_reads_api_gateway_context():
    event = {"requestContext": {"requestId": _GUID_A.upper()}}
    assert extract_request_id(event) == _GUID_A.lower()


def test_pull_request_synchronize_deliveries_differ():
    base = {
        "event_type": "pull_request",
        "trigger_id": "example-trigger",
        "pr_number": 1,
        "commit_hash": "a" * 40,
    }
    first = derive_run_id({**base, "delivery_id": _GUID_A})
    second = derive_run_id({**base, "delivery_id": _GUID_B})
    assert first != second
    assert _GUID_A not in first and _GUID_B not in second


def test_same_delivery_retry_is_stable():
    webhook = {
        "event_type": "pull_request",
        "trigger_id": "example-trigger",
        "pr_number": 1,
        "delivery_id": validate_delivery_id(_GUID_A),
    }
    assert derive_run_id(webhook) == derive_run_id(webhook)


def test_distinct_issue_comments_differ():
    base = {"event_type": "issue_comment", "trigger_id": "trigger", "pr_number": 7}
    first = derive_run_id({**base, "comment_id": 100})
    second = derive_run_id({**base, "comment_id": 200})
    assert first != second


def test_fallback_without_delivery_metadata_is_deterministic():
    webhook = {
        "event_type": "pull_request",
        "trigger_id": "trigger",
        "pr_number": 1,
        "action": "synchronize",
        "repo_name": "org/repo",
        "commit_hash": "b" * 40,
    }
    assert derive_run_id(webhook) == derive_run_id(webhook)


def test_same_pr_same_sha_without_header_uses_distinct_request_ids():
    base = {
        "event_type": "pull_request",
        "trigger_id": "trigger",
        "pr_number": 1,
        "action": "synchronize",
        "repo_name": "org/repo",
        "commit_hash": "c" * 40,
    }
    first = derive_run_id({**base, "ingress_request_id": _GUID_A})
    second = derive_run_id({**base, "ingress_request_id": _GUID_B})
    assert first != second


def test_invalid_pr_number_fails_loud():
    with pytest.raises(InvalidInvocationIdentityError, match="pr_number"):
        derive_run_id(
            {
                "event_type": "pull_request",
                "trigger_id": "trigger",
                "pr_number": "not-a-number",
                "delivery_id": _GUID_A,
            }
        )


def test_execution_id_respects_sts_bounds_for_max_sizes():
    run_id = derive_run_id(
        {
            "event_type": "pull_request",
            "trigger_id": "a" * 64,
            "pr_number": 9999999999,
            "delivery_id": _GUID_B,
        }
    )
    folder = "terraform/" + ("x" * 200)
    execution_id = compose_execution_id(run_id, folder, 0)
    assert 2 <= len(execution_id) <= 64
    assert re.fullmatch(r"[\w+=,.@-]+", execution_id)
