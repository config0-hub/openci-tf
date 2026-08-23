"""Pytest collection boundaries for immutable repository fixtures."""

import os

import pytest

collect_ignore_glob = ["fixtures/**/tests/test_*.py"]


@pytest.fixture(autouse=True)
def _stub_run_registry_lookup_for_unit_tests(monkeypatch):
    """Unit tests set RUN_REGISTRY_TABLE_NAME without AWS; default to no PR context."""

    def _get_run(_run_id: str):
        if os.environ.get("RUN_REGISTRY_TABLE_NAME"):
            return None
        raise AssertionError("unexpected run registry lookup without RUN_REGISTRY_TABLE_NAME")

    monkeypatch.setattr(
        "src.domain.engine.run_artifact_layout.get_run",
        _get_run,
    )
