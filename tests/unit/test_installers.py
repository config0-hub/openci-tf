# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
import subprocess
import tarfile
from pathlib import Path

import pytest

from src.domain.cmd_builder.installers import (
    PINNED_UPSTREAM_URLS,
    cache_key,
    env_suffix,
    render_installer,
    require_pinned_installer,
)


def _archive(tmp_path: Path) -> Path:
    binary = tmp_path / "tofu"
    binary.write_text("#!/usr/bin/env bash\n")
    binary.chmod(0o755)
    archive = tmp_path / "tofu.tar.gz"
    with tarfile.open(archive, "w:gz") as contents:
        contents.add(binary, arcname="tofu")
    return archive


def _curl(tmp_path: Path, archive: Path, cache_exit: int) -> None:
    curl = tmp_path / "curl"
    curl.write_text(f'''#!/usr/bin/env bash
is_cache=false
[[ "$*" == *"cache"* ]] && is_cache=true
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    if $is_cache && [ {cache_exit} -ne 0 ]; then exit {cache_exit}; fi
    cp "{archive}" "$2"; exit 0
  fi
  shift
done
''')
    curl.chmod(0o755)


def test_installer_version_selects_a_versioned_archive():
    old_version = render_installer("tofu", "1.8.0", "lambda", "0" * 64)
    new_version = render_installer("tofu", "1.9.0", "lambda", "0" * 64)

    assert 'archive="$BIN_DIR/tofu-1.8.0.download"' in old_version
    assert 'archive="$BIN_DIR/tofu-1.9.0.download"' in new_version
    assert "UPSTREAM_URL_TOFU_1_8_0" in old_version
    assert "UPSTREAM_URL_TOFU_1_9_0" in new_version
    assert old_version != new_version


def test_pinned_runtime_downloads_resolve_exact_url_checksum_and_cache_key():
    expected = {
        ("terraform", "1.8.5"): (
            "https://releases.hashicorp.com/terraform/1.8.5/terraform_1.8.5_linux_amd64.zip",
            "bb1ee3e8314da76658002e2e584f2d8854b6def50b7f124e27b957a42ddacfea",
        ),
        ("terraform", "1.9.8"): (
            "https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip",
            "186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8",
        ),
        ("tofu", "1.8.0"): (
            "https://github.com/opentofu/opentofu/releases/download/v1.8.0/tofu_1.8.0_linux_amd64.tar.gz",
            "cb54a998eae5dc5890a8d1adacf9b6fe396a57fc6257a9154ccdebb3035b63b8",
        ),
        ("tofu", "1.9.0"): (
            "https://github.com/opentofu/opentofu/releases/download/v1.9.0/tofu_1.9.0_linux_amd64.tar.gz",
            "48b1e2ec8dd23c107d350432b8d73a4393ef014f8eaee063bdf1d8f481083a42",
        ),
    }

    for (binary, version), (url, sha256) in expected.items():
        assert PINNED_UPSTREAM_URLS[f"{binary}:{version}"] == url
        assert require_pinned_installer(binary, version).sha256 == sha256
        assert cache_key(binary, version) == f"cache/{binary}/{version}"
        assert env_suffix(binary, version) == f"{binary}_{version}".replace(".", "_").upper()


def test_unpinned_installer_is_rejected_clearly():
    with pytest.raises(ValueError, match="unsupported unpinned installer terraform:1.10.0"):
        require_pinned_installer("terraform", "1.10.0")


def test_installer_executes_cache_hit_path(tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    _curl(tmp_path, archive, 0)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("CACHE_PUT_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("UPSTREAM_URL_TOFU", "https://upstream/tofu")
    subprocess.run(["bash", "-c", "set -e\n" + render_installer("tofu", "1.8.0", "lambda", "0" * 64)], check=True)
    assert Path("/tmp/lambda/bin/tofu").exists()


def test_installer_fails_on_real_sha256_mismatch(tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    _curl(tmp_path, archive, 22)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("CACHE_PUT_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("UPSTREAM_URL_TOFU", "https://upstream/tofu")
    completed = subprocess.run(["bash", "-c", "set -e\n" + render_installer("tofu", "1.8.0", "lambda", "0" * 64)], text=True, capture_output=True, check=False)
    assert completed.returncode != 0
    assert "FAILED" in completed.stdout
