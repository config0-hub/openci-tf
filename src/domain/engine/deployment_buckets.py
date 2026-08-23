# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Foundation bucket naming used for outer-state and child-output budgeting."""
from __future__ import annotations

from src.domain.engine.artifact_limits import MAX_DEPLOYMENT_NAME_PREFIX_CHARS

# Representative twelve-digit account id for static budgeting probes.
BUDGET_ACCOUNT_ID = "123456789012"


def foundation_bucket_names(name_prefix: str, account_id: str = BUDGET_ACCOUNT_ID) -> dict[str, str]:
    """Return tmp/done/package bucket names for one foundation name prefix."""
    prefix = name_prefix.strip()
    if not prefix:
        raise ValueError("name_prefix is required")
    if len(prefix) > MAX_DEPLOYMENT_NAME_PREFIX_CHARS:
        raise ValueError(
            f"name_prefix exceeds supported installer bound ({MAX_DEPLOYMENT_NAME_PREFIX_CHARS} characters)"
        )
    return {
        "tmp": f"{prefix}-tmp-{account_id}",
        "done": f"{prefix}-done-{account_id}",
        "package": f"{prefix}-package-{account_id}",
    }


def maximum_foundation_bucket_names(account_id: str = BUDGET_ACCOUNT_ID) -> dict[str, str]:
    """Worst-case foundation buckets at the supported installer name bound."""
    return foundation_bucket_names("p" * MAX_DEPLOYMENT_NAME_PREFIX_CHARS, account_id)


def default_foundation_bucket_names(account_id: str = BUDGET_ACCOUNT_ID) -> dict[str, str]:
    """Default installer bucket names."""
    return foundation_bucket_names("openci-tf", account_id)
