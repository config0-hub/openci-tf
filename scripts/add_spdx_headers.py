#!/usr/bin/env python3
"""Add SPDX headers to source files that do not already have them."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPYRIGHT = "Config0, Inc."
LICENSE = "AAGPL-3.0-or-later"
MARKER = "SPDX-License-Identifier"

SKIP_PARTS = {
    "tests/fixtures/live-smoke/sample-target-repo",
    "node_modules",
    ".git",
}

EXTENSION_COMMENT = {
    ".py": "#",
    ".ts": "//",
    ".tsx": "//",
    ".tf": "#",
}


def should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return any(part in rel for part in SKIP_PARTS)


def header_lines(prefix: str) -> list[str]:
    return [
        f"{prefix} SPDX-FileCopyrightText: 2026 {COPYRIGHT}",
        f"{prefix} SPDX-License-Identifier: {LICENSE}",
    ]


def already_has_header(text: str) -> bool:
    return MARKER in text


def insert_header(path: Path, prefix: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if already_has_header(text):
        return False

    lines = header_lines(prefix)
    block = "\n".join(lines) + "\n"

    if path.suffix == ".py":
        if text.startswith("#!"):
            first_nl = text.find("\n")
            if first_nl == -1:
                new_text = text + "\n" + block
            else:
                new_text = text[: first_nl + 1] + block + text[first_nl + 1 :]
        else:
            new_text = block + text
    else:
        new_text = block + text

    path.write_text(new_text, encoding="utf-8")
    return True


def main() -> None:
    updated = 0
    for ext, prefix in EXTENSION_COMMENT.items():
        for path in ROOT.rglob(f"*{ext}"):
            if should_skip(path):
                continue
            if insert_header(path, prefix):
                updated += 1
                print(path.relative_to(ROOT))
    print(f"updated {updated} files")


if __name__ == "__main__":
    main()
