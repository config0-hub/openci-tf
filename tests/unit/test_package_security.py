# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security tests for package archive construction."""

import os
import zipfile
from pathlib import Path

import pytest

from src.core.errors import ConfigResolutionError
from src.platform.git.package import (
    _RESERVED_MEMBER_BASENAMES,
    PackagePathError,
    build_package,
    validate_reserved_package_names,
)


def _write_package(tmp_path: Path, layout: dict[str, str | None]) -> str:
    root = tmp_path / "root"
    root.mkdir()
    for relative, content in layout.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            os.symlink(path.parent / "target", path)
        else:
            path.write_text(content)
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("cipher")
    destination = tmp_path / "package.zip"
    return build_package(str(root), str(destination), "#!/bin/sh", str(encrypted))


def test_package_rejects_relative_symlink(tmp_path):
    (tmp_path / "root").mkdir()
    root = tmp_path / "root"
    (root / "target").write_text("secret")
    (root / "link.tf").symlink_to("target")
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("cipher")
    with pytest.raises(PackagePathError, match="symlink rejected"):
        build_package(str(root), str(tmp_path / "package.zip"), "#!/bin/sh", str(encrypted))


def test_package_rejects_absolute_symlink(tmp_path):
    outside = tmp_path / "outside-secret"
    outside.write_text("outside")
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape.tf").symlink_to(outside)
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("cipher")
    with pytest.raises(PackagePathError, match="symlink rejected"):
        build_package(str(root), str(tmp_path / "package.zip"), "#!/bin/sh", str(encrypted))


def test_package_rejects_directory_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tf").write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "main.tf").write_text("terraform {}")
    (root / "linked-dir").symlink_to(outside)
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("cipher")
    destination = tmp_path / "package.zip"
    with pytest.raises(PackagePathError, match="symlink rejected"):
        build_package(str(root), str(destination), "#!/bin/sh", str(encrypted))
    assert not destination.exists()


def test_symlink_target_bytes_never_enter_archive(tmp_path):
    secret = "super-secret-token"
    (tmp_path / "secret").write_text(secret)
    root = tmp_path / "root"
    root.mkdir()
    (root / "main.tf").write_text("resource {}")
    (root / "leak.tf").symlink_to(tmp_path / "secret")
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("cipher")
    destination = tmp_path / "package.zip"
    with pytest.raises(PackagePathError):
        build_package(str(root), str(destination), "#!/bin/sh", str(encrypted))
    assert not destination.exists()


def test_check_use_swap_is_rejected_and_partial_archive_removed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    victim = root / "main.tf"
    victim.write_text("safe")
    outside = tmp_path / "outside-secret"
    outside.write_text("leaked-secret")
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text("cipher")
    destination = tmp_path / "package.zip"
    original_open = os.open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and str(path) == str(victim):
            swapped = True
            victim.unlink()
            victim.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises((PackagePathError, OSError)):
        build_package(str(root), str(destination), "#!/bin/sh", str(encrypted))
    assert not destination.exists()


@pytest.mark.parametrize(
    "relative",
    ["openci_tf_run.sh", "nested/module/secrets.enc.json"],
)
def test_reserved_generated_member_name_is_rejected_anywhere(tmp_path, relative):
    root = tmp_path / "root"
    reserved = root / relative
    reserved.parent.mkdir(parents=True, exist_ok=True)
    reserved.write_text("repository-controlled")
    encrypted = tmp_path / "encrypted"
    encrypted.write_text("cipher")
    destination = tmp_path / "package.zip"

    with pytest.raises(ConfigResolutionError, match="reserved package name"):
        build_package(
            str(root),
            str(destination),
            "#!/bin/sh\n",
            str(encrypted),
        )

    assert not destination.exists()


def test_outer_preflight_reports_reserved_name_as_configuration_error(tmp_path):
    root = tmp_path / "root"
    collision = root / "module" / "openci_tf_run.sh"
    collision.parent.mkdir(parents=True)
    collision.write_text("repository-controlled")

    with pytest.raises(ConfigResolutionError, match="module/openci_tf_run.sh"):
        validate_reserved_package_names(str(root))


def test_regular_files_are_packaged_without_changing_generated_members(tmp_path):
    archive = _write_package(
        tmp_path,
        {"main.tf": "terraform {}", "modules/network.tf": "resource {}"},
    )
    with zipfile.ZipFile(archive) as contents:
        assert set(contents.namelist()) == {
            "main.tf",
            "modules/network.tf",
            "openci_tf_run.sh",
            "secrets.enc.json",
        }
        assert contents.read("main.tf") == b"terraform {}"
        assert contents.read("modules/network.tf") == b"resource {}"
        assert contents.read("openci_tf_run.sh") == b"#!/bin/sh"
        assert contents.read("secrets.enc.json") == b"cipher"


@pytest.mark.parametrize("reserved", sorted(_RESERVED_MEMBER_BASENAMES))
@pytest.mark.parametrize("shape", ["directory", "symlink"])
def test_reserved_name_rejected_for_every_entry_type(tmp_path, reserved, shape):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "main.tf").write_text("# ok\n", encoding="utf-8")
    target = folder / "nested"
    target.mkdir()
    if shape == "directory":
        inner = target / reserved
        inner.mkdir()
        (inner / "x.tf").write_text("# hidden\n", encoding="utf-8")
    else:
        (target / reserved).symlink_to(folder / "main.tf")
    with pytest.raises(ConfigResolutionError, match="reserved package name"):
        validate_reserved_package_names(str(folder))
