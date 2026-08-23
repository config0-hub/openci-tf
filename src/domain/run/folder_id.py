"""Opaque folder identifiers for API routes with nested paths."""
from __future__ import annotations

import base64
import re

from src.core.registry_schema import normalize_folder_path

_FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,344}$")
_TRAVERSAL = re.compile(r"(^|/)\.\.(/|$)|(^|/)\./")


def _normalize_folder(folder: str) -> str:
    normalized = normalize_folder_path(folder)
    if _TRAVERSAL.search(normalized):
        raise ValueError("invalid folder path")
    if normalized.startswith("/") or normalized.endswith("/"):
        raise ValueError("invalid folder path")
    return normalized


def encode_folder_id(folder: str) -> str:
    normalized = _normalize_folder(folder)
    return base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")


def decode_folder_id(folder_id: str) -> str:
    if not _FOLDER_ID.fullmatch(folder_id):
        raise ValueError("invalid folder_id")
    padding = "=" * (-len(folder_id) % 4)
    try:
        decoded = base64.urlsafe_b64decode(folder_id + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("invalid folder_id") from error
    return _normalize_folder(decoded)


def folder_matches(folder_id: str, stored_folder: str) -> bool:
    try:
        return decode_folder_id(folder_id) == _normalize_folder(stored_folder)
    except ValueError:
        return False
