"""Configured Terraform folder discovery for pinned repository checkouts."""
from __future__ import annotations

from pathlib import Path

from src.core.errors import ConfigResolutionError
from src.core.registry_schema import normalize_folder_path

CONFIG_PATH = Path(".openci_tf/config.yaml")


def _canonical_folder_key(physical_path: str) -> str:
    try:
        return normalize_folder_path(physical_path)
    except ValueError as error:
        raise ConfigResolutionError(str(error)) from error


def discover_folder_paths(root: Path) -> dict[str, str]:
    """Map canonical NFC folder keys to physical relative paths beneath a clone."""
    canonical_to_physical: dict[str, str] = {}
    for path in root.rglob(str(CONFIG_PATH)):
        folder_path = path.parent.parent
        if folder_path == root:
            continue
        physical = folder_path.relative_to(root).as_posix()
        canonical = _canonical_folder_key(physical)
        prior = canonical_to_physical.get(canonical)
        if prior is not None and prior != physical:
            raise ConfigResolutionError(
                f"canonical folder collision between {prior!r} and {physical!r}"
            )
        canonical_to_physical[canonical] = physical
    return canonical_to_physical


def discover_folders(root: Path) -> list[str]:
    """Return canonical NFC folder keys for configured Terraform folders."""
    return sorted(discover_folder_paths(root).keys())
