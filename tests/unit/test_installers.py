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
    old_version = render_installer("tofu", "1.10.6", "lambda", "0" * 64)
    new_version = render_installer("tofu", "1.12.6", "lambda", "0" * 64)

    assert 'archive="$BIN_DIR/tofu-1.10.6.download"' in old_version
    assert 'archive="$BIN_DIR/tofu-1.12.6.download"' in new_version
    assert "UPSTREAM_URL_TOFU_1_10_6" in old_version
    assert "UPSTREAM_URL_TOFU_1_12_6" in new_version
    assert old_version != new_version
    assert 'curl --fail-with-body --show-error --location "$upstream_url" -o "$archive"' in old_version
    assert 'curl --fail-with-body --show-error "$cache_get_url" -o "$archive"' in old_version
    assert 'curl --fail-with-body --show-error -H \'Content-Type: application/octet-stream\' --upload-file "$archive" "$cache_put_url"' in old_version
    put_lines = [line for line in old_version.splitlines() if '--upload-file "$archive"' in line and "cache_put_url" in line]
    assert put_lines
    assert all("--location" not in line for line in put_lines)


def test_pinned_runtime_downloads_resolve_exact_url_checksum_and_cache_key():
    expected = {
        ("terraform", "1.10.5"): (
            "https://releases.hashicorp.com/terraform/1.10.5/terraform_1.10.5_linux_amd64.zip",
            "0566a24f5332098b15716ebc394be503f4094acba5ba529bf5eb0698ed5e2a90",
        ),
        ("terraform", "1.12.2"): (
            "https://releases.hashicorp.com/terraform/1.12.2/terraform_1.12.2_linux_amd64.zip",
            "1eaed12ca41fcfe094da3d76a7e9aa0639ad3409c43be0103ee9f5a1ff4b7437",
        ),
        ("tofu", "1.10.6"): (
            "https://github.com/opentofu/opentofu/releases/download/v1.10.6/tofu_1.10.6_linux_amd64.tar.gz",
            "b6b46b4fd8dd0b96e624f2a2d5fbc4efae2fc0174529b37292775c847c2e7d2c",
        ),
        ("tofu", "1.12.6"): (
            "https://github.com/opentofu/opentofu/releases/download/v1.12.6/tofu_1.12.6_linux_amd64.tar.gz",
            "50a6106fa4de523d09c87af85f3db1dd47535fc005727fdca6852146476b88ec",
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
    subprocess.run(["bash", "-c", "set -e\n" + render_installer("tofu", "1.10.6", "lambda", "0" * 64)], check=True)
    assert Path("/tmp/lambda/bin/tofu").exists()


def test_installer_fails_on_real_sha256_mismatch(tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    _curl(tmp_path, archive, 22)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("CACHE_PUT_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("UPSTREAM_URL_TOFU", "https://upstream/tofu")
    completed = subprocess.run(["bash", "-c", "set -e\n" + render_installer("tofu", "1.10.6", "lambda", "0" * 64)], text=True, capture_output=True, check=False)
    assert completed.returncode != 0
    assert "FAILED" in completed.stdout
