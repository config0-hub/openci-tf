"""Normalize DynamoDB resource items into JSON-safe Python values."""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def _integral_decimal(value: Decimal) -> int:
    if value != value.to_integral_value():
        raise ValueError("DynamoDB number must be integral")
    return int(value)


def normalize_dynamo_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _integral_decimal(value)
    if isinstance(value, dict):
        return {str(key): normalize_dynamo_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_dynamo_value(item) for item in value]
    return value


def normalize_dynamo_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return normalize_dynamo_value(item)
