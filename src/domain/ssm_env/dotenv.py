"""Parse and validate dotenv text for hub SSM SecureString parameters."""

from __future__ import annotations

import re

from src.core.errors import SsmEnvError

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ENV_VARS = 64
_MAX_VALUE_BYTES = 4096
_MAX_PARAMETER_BYTES = 4096
_MAX_MERGED_ENV_VARS = 64
_MAX_MERGED_TOTAL_VALUE_BYTES = 16384

_PROTECTED_EXACT = frozenset({"PATH", "HOME", "INFRACOST_API_KEY"})
_PROTECTED_PREFIXES = ("AWS_", "ARTIFACT_", "CACHE_", "UPSTREAM_", "SOPS_")
_PROTECTED_GIT_NAMES = frozenset({"GIT_ASKPASS", "GIT_TERMINAL_PROMPT", "SSH_ASKPASS"})


def is_protected_env_name(name: str) -> bool:
    """Return whether an environment variable name must not come from SSM dotenv."""
    if name in _PROTECTED_EXACT:
        return True
    if name in _PROTECTED_GIT_NAMES:
        return True
    if name.startswith("GIT_CONFIG_"):
        return True
    return any(name.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


def _unquote(value: str, *, source: str, line_number: int) -> str:
    if not value or value[0] not in {"'", '"'}:
        return value
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise SsmEnvError(f"{source}: mismatched quotes on line {line_number}")
    body = value[1:-1]
    if quote == '"':
        return body.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
    return body


def parse_dotenv(text: str, *, source: str = "dotenv") -> dict[str, str]:
    """Parse complete dotenv text into a validated mapping.

    Supported syntax:
    - blank lines and full-line ``#`` comments are ignored
    - optional ``export `` prefix
    - the first ``=`` separates key and value
    - single- or double-quoted values with basic escapes in double quotes
    """
    if not isinstance(text, str):
        raise SsmEnvError(f"{source}: dotenv content must be a string")
    if "\x00" in text:
        raise SsmEnvError(f"{source}: dotenv content contains NUL bytes")
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_PARAMETER_BYTES:
        raise SsmEnvError(f"{source}: dotenv content exceeds {_MAX_PARAMETER_BYTES} bytes")

    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise SsmEnvError(f"{source}: malformed dotenv line {line_number}")
        key, value = line.split("=", 1)
        name = key.strip()
        if not name or not _ENV_NAME.fullmatch(name):
            raise SsmEnvError(f"{source}: invalid environment variable name on line {line_number}")
        if is_protected_env_name(name):
            raise SsmEnvError(f"{source}: protected environment variable {name}")
        raw_value = value.strip()
        decoded = _unquote(raw_value, source=source, line_number=line_number)
        if "\x00" in decoded:
            raise SsmEnvError(f"{source}: value for {name} contains NUL bytes")
        if len(decoded.encode("utf-8")) > _MAX_VALUE_BYTES:
            raise SsmEnvError(f"{source}: value for {name} exceeds {_MAX_VALUE_BYTES} bytes")
        if name in parsed:
            raise SsmEnvError(f"{source}: duplicate environment variable {name}")
        parsed[name] = decoded
        if len(parsed) > _MAX_ENV_VARS:
            raise SsmEnvError(f"{source}: dotenv exceeds {_MAX_ENV_VARS} variables")
    return parsed
