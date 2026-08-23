"""Pure AWS resource identifier validation shared across layers."""
from __future__ import annotations

import re

_CODEBUILD_BUILD_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def is_valid_codebuild_build_id(build_id: str) -> bool:
    return isinstance(build_id, str) and bool(_CODEBUILD_BUILD_ID.fullmatch(build_id))
