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
    # Platform-driven runs pass -backend-config=use_lockfile=true at init, so
    # every pinned runtime must support the S3 native lock file (>= 1.10).
    ("terraform", "1.10.5"): PinnedInstaller(
        url="https://releases.hashicorp.com/terraform/1.10.5/terraform_1.10.5_linux_amd64.zip",
        sha256="0566a24f5332098b15716ebc394be503f4094acba5ba529bf5eb0698ed5e2a90",
    ),
    ("terraform", "1.12.2"): PinnedInstaller(
        url="https://releases.hashicorp.com/terraform/1.12.2/terraform_1.12.2_linux_amd64.zip",
        sha256="1eaed12ca41fcfe094da3d76a7e9aa0639ad3409c43be0103ee9f5a1ff4b7437",
    ),
    ("tofu", "1.10.6"): PinnedInstaller(
        url="https://github.com/opentofu/opentofu/releases/download/v1.10.6/tofu_1.10.6_linux_amd64.tar.gz",
        sha256="b6b46b4fd8dd0b96e624f2a2d5fbc4efae2fc0174529b37292775c847c2e7d2c",
    ),
    ("tofu", "1.12.6"): PinnedInstaller(
        url="https://github.com/opentofu/opentofu/releases/download/v1.12.6/tofu_1.12.6_linux_amd64.tar.gz",
        sha256="50a6106fa4de523d09c87af85f3db1dd47535fc005727fdca6852146476b88ec",
    ),
}

# S3 native lock file support (backend use_lockfile) requires >= 1.10 on both
# tofu and terraform; platform-driven runs always lock, so older runtimes are
# rejected outright instead of running unlocked.
MIN_LOCKFILE_RUNTIME = (1, 10)

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
    major, minor = (int(part) for part in version.split(".")[:2])
    if (major, minor) < MIN_LOCKFILE_RUNTIME:
        raise ValueError(
            f"tf_runtime {runtime} predates S3 native state locking; "
            f"platform runs require {'.'.join(str(part) for part in MIN_LOCKFILE_RUNTIME)} or newer"
        )
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
  curl --fail-with-body --show-error --location "$upstream_url" -o "$archive"
  echo "{checksum}  $archive" | sha256sum -c -
  curl --fail-with-body --show-error -H 'Content-Type: application/octet-stream' --upload-file "$archive" "$cache_put_url"
fi
{extract}
installed="$extract_dir/{binary}"
if [ ! -f "$installed" ]; then installed="$extract_dir/{binary}-linux-amd64"; fi
test -f "$installed"
mv "$installed" "$BIN_DIR/{binary}"
rm -rf "$extract_dir"
chmod +x "$BIN_DIR/{binary}"
'''
