# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every executor resource that lost its count gate has a moved block from [0]."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESOURCE_RE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)
_MOVED_RE = re.compile(r"moved\s*\{\s*from\s*=\s*([^\s]+)\s*to\s*=\s*([^\s]+)\s*\}", re.MULTILINE)


def _resources_without_count(source: str, name_prefix: str) -> set[str]:
    addresses: set[str] = set()
    for match in _RESOURCE_RE.finditer(source):
        resource_type, name = match.groups()
        if not name.startswith(name_prefix):
            continue
        block_start = match.end()
        depth = 1
        index = block_start
        while depth and index < len(source):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            index += 1
        body = source[block_start:index]
        if re.search(r"^\s*count\s*=", body, re.MULTILINE) is None:
            addresses.add(f"{resource_type}.{name}")
    return addresses


@pytest.mark.parametrize(
    "module_dir,resource_file,moved_file,name_prefix",
    [
        ("infra/modules/hub-setup", "local_executor.tf", "executor_local_moved.tf", "executor_local"),
        ("infra/modules/target-connect", "main.tf", "executor_remote_moved.tf", "executor_remote"),
    ],
)
def test_count_removed_resources_have_reverse_moved_blocks(
    module_dir: str, resource_file: str, moved_file: str, name_prefix: str
) -> None:
    module = _REPO_ROOT / module_dir
    resources = _resources_without_count((module / resource_file).read_text(encoding="utf-8"), name_prefix)
    assert resources, "expected bare executor resources"
    moved = {frm: to for frm, to in _MOVED_RE.findall((module / moved_file).read_text(encoding="utf-8"))}
    for address in sorted(resources):
        assert moved.get(f"{address}[0]") == address, f"missing moved block for {address}"
