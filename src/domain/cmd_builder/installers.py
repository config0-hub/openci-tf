# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shell fragments for deterministic binary installation."""

from __future__ import annotations

import re
from dataclasses import dataclass

_RUNTIME_BINARIES = frozenset({"terraform", "tofu"})
_TOOL_BINARIES = frozenset({"tfsec", "infracost"})
_SUPPORTED_BINARIES = _RUNTIME_BINARIES | _TOOL_BINARIES


@dataclass(frozen=True)
class PinnedInstaller:
    url: str
    sha256: str


PINNED_INSTALLERS: dict[tuple[str, str], PinnedInstaller] = {
    ("tfsec", "1.28.10"): PinnedInstaller(
        url="https://github.com/aquasecurity/tfsec/releases/download/v1.28.10/tfsec_1.28.10_linux_amd64.tar.gz",
        sha256="16601d830bf13590cf2e9537e48d1a9c33f87b2f715f46e359f93fc4457320bc",
    ),
    ("infracost", "0.10.39"): PinnedInstaller(
        url="https://github.com/infracost/infracost/releases/download/v0.10.39/infracost-linux-amd64.tar.gz",
        sha256="4c23dc9de85bd16832a3ab9b2f5b48d24255af3df410ad8aab2609f4b2c47fc6",
    ),
    ("terraform", "1.8.5"): PinnedInstaller(
        url="https://releases.hashicorp.com/terraform/1.8.5/terraform_1.8.5_linux_amd64.zip",
        sha256="bb1ee3e8314da76658002e2e584f2d8854b6def50b7f124e27b957a42ddacfea",
    ),
    ("terraform", "1.9.8"): PinnedInstaller(
        url="https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip",
        sha256="186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8",
    ),
    ("tofu", "1.8.0"): PinnedInstaller(
        url="https://github.com/opentofu/opentofu/releases/download/v1.8.0/tofu_1.8.0_linux_amd64.tar.gz",
        sha256="cb54a998eae5dc5890a8d1adacf9b6fe396a57fc6257a9154ccdebb3035b63b8",
    ),
    ("tofu", "1.9.0"): PinnedInstaller(
        url="https://github.com/opentofu/opentofu/releases/download/v1.9.0/tofu_1.9.0_linux_amd64.tar.gz",
        sha256="48b1e2ec8dd23c107d350432b8d73a4393ef014f8eaee063bdf1d8f481083a42",
    ),
}

def installer_key(binary: str, version: str) -> str:
    return f"{binary}:{version}"


PINNED_SHA256 = {key: installer.sha256 for key, installer in PINNED_INSTALLERS.items()}
PINNED_UPSTREAM_URLS = {installer_key(*key): installer.url for key, installer in PINNED_INSTALLERS.items()}


def supported_runtime_keys() -> tuple[str, ...]:
    return tuple(sorted(installer_key(binary, version) for binary, version in PINNED_INSTALLERS if binary in _RUNTIME_BINARIES))


def require_pinned_installer(binary: str, version: str) -> PinnedInstaller:
    if binary not in _SUPPORTED_BINARIES:
        raise ValueError(f"unsupported installer binary: {binary}")
    installer = PINNED_INSTALLERS.get((binary, version))
    if installer is None:
        supported = ", ".join(sorted(installer_key(*key) for key in PINNED_INSTALLERS if key[0] == binary)) or "none"
        raise ValueError(f"unsupported unpinned installer {installer_key(binary, version)}; supported pinned versions: {supported}")
    return installer


def require_pinned_runtime(runtime: str) -> tuple[str, str]:
    if not isinstance(runtime, str) or ":" not in runtime:
        raise ValueError("tf_runtime must use a pinned runtime in binary:version form")
    binary, version = runtime.split(":", 1)
    if binary not in _RUNTIME_BINARIES:
        raise ValueError("tf_runtime binary must be tofu or terraform")
    try:
        require_pinned_installer(binary, version)
    except ValueError as error:
        supported = ", ".join(supported_runtime_keys())
        raise ValueError(f"unsupported unpinned tf_runtime {runtime}; supported pinned runtimes: {supported}") from error
    return binary, version


def cache_key(binary: str, version: str) -> str:
    require_pinned_installer(binary, version)
    return f"cache/{binary}/{version}"


def env_suffix(binary: str, version: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "_", installer_key(binary, version)).upper()


def bin_dir(target: str) -> str:
    if target == "lambda":
        return "/tmp/lambda/bin"
    if target == "codebuild":
        return "/usr/local/bin"
    raise ValueError(f"unknown execution target: {target}")


def _environment_name(binary: str, version: str, prefix: str) -> str:
    return f"{prefix}_{env_suffix(binary, version)}"


def _legacy_environment_name(binary: str, prefix: str) -> str:
    return f"{prefix}_{binary.upper().replace('-', '_')}"


def render_installer(binary: str, version: str, target: str, checksum: str) -> str:
    """Use a versioned archive cache, with checksum-verified upstream fallback."""
    require_pinned_installer(binary, version)
    directory = bin_dir(target)
    cache_get = _environment_name(binary, version, "CACHE_GET_URL")
    cache_put = _environment_name(binary, version, "CACHE_PUT_URL")
    upstream = _environment_name(binary, version, "UPSTREAM_URL")
    legacy_cache_get = _legacy_environment_name(binary, "CACHE_GET_URL")
    legacy_cache_put = _legacy_environment_name(binary, "CACHE_PUT_URL")
    legacy_upstream = _legacy_environment_name(binary, "UPSTREAM_URL")
    extract = "python3 -c 'import zipfile, sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \"$archive\" \"$extract_dir\"" if binary == "terraform" else "python3 -c 'import tarfile, sys; tarfile.open(sys.argv[1]).extractall(sys.argv[2], filter=\"data\")' \"$archive\" \"$extract_dir\""
    return f'''BIN_DIR={directory!r}
mkdir -p "$BIN_DIR"
archive="$BIN_DIR/{binary}-{version}.download"
extract_dir="$(mktemp -d)"
cache_get_url="${{{cache_get}:-${{{legacy_cache_get}:-}}}}"
cache_put_url="${{{cache_put}:-${{{legacy_cache_put}:-}}}}"
upstream_url="${{{upstream}:-${{{legacy_upstream}:-}}}}"
if ! curl --fail-with-body --show-error "$cache_get_url" -o "$archive"; then
  test -n "$upstream_url"
  test -n "$cache_put_url"
  curl --fail-with-body --show-error "$upstream_url" -o "$archive"
  echo "{checksum}  $archive" | sha256sum -c -
  curl --fail-with-body --show-error --upload-file "$archive" "$cache_put_url"
fi
{extract}
installed="$extract_dir/{binary}"
if [ ! -f "$installed" ]; then installed="$extract_dir/{binary}-linux-amd64"; fi
test -f "$installed"
mv "$installed" "$BIN_DIR/{binary}"
rm -rf "$extract_dir"
chmod +x "$BIN_DIR/{binary}"
'''
