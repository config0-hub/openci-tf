"""Ensure every shipped source module remains importable in the test image."""

import importlib
from pathlib import Path


def test_every_source_module_imports() -> None:
    for path in Path("src").rglob("*.py"):
        if path.name == "__init__.py":
            continue
        module = ".".join(path.with_suffix("").parts)
        importlib.import_module(module)
