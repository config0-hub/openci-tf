# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build engine package archives from a checked-out folder."""
from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path

from src.core.errors import ConfigResolutionError

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_RESERVED_MEMBER_BASENAMES = frozenset({"openci_tf_run.sh", "secrets.enc.json"})


class PackagePathError(ValueError):
    """Raised when a candidate file must not enter the package archive."""


def _relative_under_root(root: Path, path: Path) -> Path:
    return path.relative_to(root)


def _reject_symlink(root: Path, path: Path) -> None:
    if path.is_symlink():
        raise PackagePathError(f"symlink rejected: {_relative_under_root(root, path)}")


def _reject_special_entry(root: Path, path: Path) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise PackagePathError(f"symlink rejected: {_relative_under_root(root, path)}")
    if stat.S_ISDIR(mode):
        raise PackagePathError(f"directory rejected: {_relative_under_root(root, path)}")
    if not stat.S_ISREG(mode):
        raise PackagePathError(f"non-regular file rejected: {_relative_under_root(root, path)}")


def _read_regular_file_bytes(root: Path, path: Path, *, require_under_root: bool = True) -> bytes:
    _reject_special_entry(root, path)
    resolved = path.resolve()
    if require_under_root:
        try:
            resolved.relative_to(root.resolve())
        except ValueError as error:
            raise PackagePathError(f"path escapes package root: {_relative_under_root(root, path)}") from error
    flags = os.O_RDONLY | _O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PackagePathError(f"non-regular file rejected: {_relative_under_root(root, path)}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _iter_packable_files(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                relative = entry_path.relative_to(root)
                if ".git" in relative.parts:
                    continue
                # Reservation applies to EVERY entry type: a directory (or any
                # other node) named like a generated member still collides with
                # it at engine-side extraction.
                if entry_path.name in _RESERVED_MEMBER_BASENAMES:
                    raise ConfigResolutionError(
                        f"repository file uses reserved package name: {relative}"
                    )
                if entry.is_symlink():
                    raise PackagePathError(f"symlink rejected: {relative}")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry_path)
                    continue
                if entry.is_file(follow_symlinks=False):
                    yield entry_path
                    continue
                raise PackagePathError(f"non-regular file rejected: {relative}")


def validate_reserved_package_names(folder: str) -> None:
    """Reject repository files that would collide with generated archive members."""
    root = Path(folder).resolve()
    # Validation happens via exceptions raised inside the generator as it
    # walks the tree, so the loop body is intentionally empty — do not remove.
    for _path in _iter_packable_files(root):
        pass


def build_package(folder: str, destination: str, script: str, encrypted_secrets_path: str) -> str:
    root = Path(folder).resolve()
    try:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in _iter_packable_files(root):
                relative = _relative_under_root(root, path)
                archive.writestr(str(relative), _read_regular_file_bytes(root, path))
            archive.writestr("openci_tf_run.sh", script)
            archive.writestr("secrets.enc.json", _read_regular_file_bytes(root, Path(encrypted_secrets_path), require_under_root=False))
        return destination
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise
