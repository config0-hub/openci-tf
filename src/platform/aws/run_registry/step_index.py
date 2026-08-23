# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry step-index validation and ASL-to-API conversion."""

from __future__ import annotations

MAX_REGISTRY_STEP_COUNT = 20

__all__ = [
    "MAX_REGISTRY_STEP_COUNT",
    "registry_step_index_from_state",
    "validate_registry_step_count",
    "validate_registry_step_index",
    "validate_registry_step_range",
]


def validate_registry_step_index(step_index: int) -> int:
    """Validate the 1-based step index stored in registry records."""
    if type(step_index) is not int or step_index < 1:
        raise ValueError("step_index must be an integer >= 1")
    if step_index > MAX_REGISTRY_STEP_COUNT:
        raise ValueError(f"step_index must be <= {MAX_REGISTRY_STEP_COUNT}")
    return step_index


def validate_registry_step_count(step_count: int) -> int:
    """Validate the 1-based step count stored in registry records."""
    if type(step_count) is not int or step_count < 1:
        raise ValueError("step_count must be an integer >= 1")
    if step_count > MAX_REGISTRY_STEP_COUNT:
        raise ValueError(f"step_count must be <= {MAX_REGISTRY_STEP_COUNT}")
    return step_count


def validate_registry_step_range(step_index: int, step_count: int) -> tuple[int, int]:
    """Validate an indexed step and its total count for registry writes."""
    api_step_index = validate_registry_step_index(step_index)
    api_step_count = validate_registry_step_count(step_count)
    if api_step_index > api_step_count:
        raise ValueError("step_count must be an integer >= step_index")
    return api_step_index, api_step_count


def registry_step_index_from_state(raw_step_index: object) -> int:
    """Convert ASL's optional 0-based step_index to the registry's 1-based value."""
    if type(raw_step_index) is int:
        if raw_step_index < 0:
            raise ValueError("step_index must be >= 0 when present")
        return validate_registry_step_index(raw_step_index + 1)
    elif raw_step_index is None:
        return 1
    else:
        raise ValueError("step_index must be an integer when present")
