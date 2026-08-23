"""Uniform secret redaction and hard bounds for terminal evidence."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Final

MAX_TERMINAL_EVIDENCE_TEXT_CHARS: Final = 256
MAX_TERMINAL_EVIDENCE_TEXT_JSON_BYTES: Final = 260
MAX_TERMINAL_EVIDENCE_FIELDS: Final = 16
MAX_TERMINAL_EVIDENCE_ITEMS: Final = 16
MAX_TERMINAL_EVIDENCE_DEPTH: Final = 4
MAX_TERMINAL_EVIDENCE_KEY_CHARS: Final = 64

_REDACTED: Final = "***"
_TRUNCATED: Final = "..."
_MIN_INTEGER: Final = -(2**63)
_MAX_INTEGER: Final = 2**63 - 1
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_GITHUB_TOKEN_RE = re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")
_URI_CREDENTIAL_RE = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_LABELED_SECRET_RE = re.compile(
    r"(?ix)"
    r"(?P<prefix>[\"']?\b(?:"
    r"aws_access_key_id|access_key_id|aws_secret_access_key|secret_access_key|"
    r"aws_session_token|session_token|authorization|client_secret|api_key|"
    r"password|passwd|secret|token"
    r")\b[\"']?\s*[=:]\s*)"
    r"(?P<value>Bearer\s+[A-Za-z0-9._~+/=-]+|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\]]+)"
)


def _redact_text(value: str) -> str:
    text = _PRIVATE_KEY_RE.sub(_REDACTED, value)
    text = _URI_CREDENTIAL_RE.sub(r"\1***@", text)

    def replace_labeled(match: re.Match[str]) -> str:
        raw_value = match.group("value")
        if raw_value.startswith(('"', "'")):
            quote = raw_value[0]
            return f"{match.group('prefix')}{quote}{_REDACTED}{quote}"
        return f"{match.group('prefix')}{_REDACTED}"

    text = _LABELED_SECRET_RE.sub(replace_labeled, text)
    text = _BEARER_RE.sub(r"\1 ***", text)
    text = _AWS_ACCESS_KEY_RE.sub(_REDACTED, text)
    return _GITHUB_TOKEN_RE.sub(_REDACTED, text)


def _json_string_bytes(value: str) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _bound_text(value: str, *, max_chars: int) -> str:
    candidate = value[:max_chars]
    truncated = len(candidate) < len(value)
    suffix = _TRUNCATED if truncated else ""
    if suffix:
        candidate = candidate[: max(0, max_chars - len(suffix))] + suffix
    if _json_string_bytes(candidate) <= MAX_TERMINAL_EVIDENCE_TEXT_JSON_BYTES:
        return candidate
    low = 0
    high = len(candidate)
    while low < high:
        middle = (low + high + 1) // 2
        bounded = candidate[:middle] + _TRUNCATED
        if _json_string_bytes(bounded) <= MAX_TERMINAL_EVIDENCE_TEXT_JSON_BYTES:
            low = middle
        else:
            high = middle - 1
    return candidate[:low] + _TRUNCATED


def _bounded_key(value: object) -> str:
    redacted = _redact_text(str(value))
    return _bound_text(redacted, max_chars=MAX_TERMINAL_EVIDENCE_KEY_CHARS)


def _redact_and_bound(value: object, *, depth: int) -> object:
    if depth >= MAX_TERMINAL_EVIDENCE_DEPTH:
        return _TRUNCATED
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if _MIN_INTEGER <= value <= _MAX_INTEGER else _TRUNCATED
    if isinstance(value, float):
        return value if math.isfinite(value) else _TRUNCATED
    if isinstance(value, str):
        return _bound_text(
            _redact_text(value), max_chars=MAX_TERMINAL_EVIDENCE_TEXT_CHARS
        )
    if isinstance(value, Mapping):
        bounded: dict[str, object] = {}
        for raw_key, raw_value in islice(
            value.items(), MAX_TERMINAL_EVIDENCE_FIELDS
        ):
            key = _bounded_key(raw_key)
            if key in bounded:
                continue
            bounded[key] = _redact_and_bound(raw_value, depth=depth + 1)
        return bounded
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _redact_and_bound(item, depth=depth + 1)
            for item in value[:MAX_TERMINAL_EVIDENCE_ITEMS]
        ]
    return _bound_text(
        _redact_text(str(value)), max_chars=MAX_TERMINAL_EVIDENCE_TEXT_CHARS
    )


def redact_and_bound_terminal_evidence(value: object) -> object:
    """Return JSON-safe terminal evidence with secrets, fields, and counts bounded.

    Raw engine output and command artifacts remain available at their S3 pointers.
    Only this compact representation is eligible for manifests, registry outcomes,
    Step Functions state, or PR rendering.
    """

    return _redact_and_bound(value, depth=0)
