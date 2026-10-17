# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shell fragments for deterministic binary installation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_RUNTIME_BINARIES = frozenset({"terraform", "tofu"})
_TOOL_BINARIES = frozenset({"tfsec", "infracost"})
_SUPPORTED_BINARIES = _RUNTIME_BINARIES | _TOOL_BINARIES

# A tf_runtime is binary:SemVer. The version lands in the generated bash, the
# installer cache key and the release URLs, so nothing else is accepted.
RUNTIME_PATTERN = re.compile(r"^(tofu|terraform):\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class PinnedInstaller:
    url: str
    sha256: str


# The scan tools are pinned by sha256. A tofu or terraform runtime is not: any
# version installs from its own release, checked against that release's
# SHA256SUMS file (runtime_release, render_installer).
PINNED_INSTALLERS: dict[tuple[str, str], PinnedInstaller] = {
    ("tfsec", "1.28.10"): PinnedInstaller(
        url="https://github.com/aquasecurity/tfsec/releases/download/v1.28.10/tfsec_1.28.10_linux_amd64.tar.gz",
        sha256="16601d830bf13590cf2e9537e48d1a9c33f87b2f715f46e359f93fc4457320bc",
    ),
    ("infracost", "0.10.39"): PinnedInstaller(
        url="https://github.com/infracost/infracost/releases/download/v0.10.39/infracost-linux-amd64.tar.gz",
        sha256="4c23dc9de85bd16832a3ab9b2f5b48d24255af3df410ad8aab2609f4b2c47fc6",
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


@dataclass(frozen=True)
class RuntimeRelease:
    """A tofu or terraform release: its linux_amd64 archive and its checksum file."""

    url: str
    sums_url: str
    archive: str


def require_pinned_installer(binary: str, version: str) -> PinnedInstaller:
    if binary not in _SUPPORTED_BINARIES:
        raise ValueError(f"unsupported installer binary: {binary}")
    installer = PINNED_INSTALLERS.get((binary, version))
    if installer is None:
        supported = ", ".join(sorted(installer_key(*key) for key in PINNED_INSTALLERS if key[0] == binary)) or "none"
        raise ValueError(f"unsupported unpinned installer {installer_key(binary, version)}; supported pinned versions: {supported}")
    return installer


def require_runtime(runtime: str) -> tuple[str, str]:
    """Any tofu or terraform version in binary:version form, from 1.10 on."""
    if not isinstance(runtime, str) or not RUNTIME_PATTERN.fullmatch(runtime):
        raise ValueError(
            f"unsupported tf_runtime {runtime!r}; use binary:version with binary tofu "
            "or terraform and a SemVer version"
        )
    binary, version = runtime.split(":", 1)
    major, minor = (int(part) for part in version.split(".")[:2])
    if (major, minor) < MIN_LOCKFILE_RUNTIME:
        raise ValueError(
            f"tf_runtime {runtime} predates S3 native state locking; "
            f"platform runs require {'.'.join(str(part) for part in MIN_LOCKFILE_RUNTIME)} or newer"
        )
    return binary, version


def _require_installer(binary: str, version: str) -> None:
    if binary in _RUNTIME_BINARIES:
        require_runtime(installer_key(binary, version))
    else:
        require_pinned_installer(binary, version)


def require_upstream_url_key(key: str) -> None:
    """A registered ``upstream_urls`` key: a pinned scan tool or a tf_runtime."""
    if key in PINNED_UPSTREAM_URLS:
        return
    try:
        require_runtime(key)
    except ValueError as error:
        supported = ", ".join(sorted(PINNED_UPSTREAM_URLS))
        raise ValueError(f"is neither a pinned tool key ({supported}) nor a tf_runtime: {error}") from error


def runtime_release(binary: str, version: str) -> RuntimeRelease:
    """The release openci-tf installs a tofu or terraform version from.

    tofu ships a .tar.gz on its GitHub release, terraform a .zip on
    releases.hashicorp.com. Each release publishes one SHA256SUMS file beside
    its archives.
    """
    require_runtime(installer_key(binary, version))
    if binary == "terraform":
        base = f"https://releases.hashicorp.com/terraform/{version}"
        archive = f"terraform_{version}_linux_amd64.zip"
    else:
        base = f"https://github.com/opentofu/opentofu/releases/download/v{version}"
        archive = f"tofu_{version}_linux_amd64.tar.gz"
    return RuntimeRelease(url=f"{base}/{archive}", sums_url=f"{base}/{binary}_{version}_SHA256SUMS", archive=archive)


def upstream_url(binary: str, version: str, registered: Mapping[str, object], *, bare_key: bool) -> str:
    """The download URL of one installer.

    The repository's registered ``binary:version`` URL wins, then its bare
    ``binary`` URL when ``bare_key`` (the run needs one version of that
    binary). A tofu or terraform runtime with neither downloads from its own
    release. Whatever the URL, render_installer checks the archive.
    """
    _require_installer(binary, version)
    key = installer_key(binary, version)
    url = registered.get(key)
    if url is None and bare_key:
        url = registered.get(binary)
    if url is None and binary in _RUNTIME_BINARIES:
        url = runtime_release(binary, version).url
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"upstream_urls missing a valid URL for installer {key}")
    return url


def cache_key(binary: str, version: str) -> str:
    _require_installer(binary, version)
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


def _expected_sha256(binary: str, version: str) -> str:
    """Shell lines that set ``expected`` to the archive's sha256.

    A scan tool's sha256 is pinned. A runtime's comes from the entry for its
    release archive in that release's SHA256SUMS file. A missing file or a
    missing entry fails the script.
    """
    if binary not in _RUNTIME_BINARIES:
        return f"expected={require_pinned_installer(binary, version).sha256}"
    release = runtime_release(binary, version)
    return f'''sums="$(mktemp)"
  curl --fail-with-body --show-error --location {release.sums_url!r} -o "$sums"
  expected="$(python3 -c 'import sys; print(*[line.split()[0] for line in open(sys.argv[1]) if line.split()[1:] == [sys.argv[2]]])' "$sums" {release.archive!r})"
  rm -f "$sums"
  if [ -z "$expected" ]; then
    echo "Error: {release.sums_url} lists no {release.archive}" >&2
    exit 1
  fi'''


def render_installer(binary: str, version: str, target: str) -> str:
    """Use a versioned archive cache, with checksum-verified upstream fallback.

    A cache miss downloads the archive and checks its sha256 before the cache
    PUT and before use (``_expected_sha256``), so the cache only ever holds a
    checked archive.

    The cache GET and PUT call ``s3_curl``, which the generated script defines
    (``script_generator.S3_CURL_HELPER``) so S3 throttling is retried.
    """
    _require_installer(binary, version)
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
if ! s3_curl "$archive" "$cache_get_url"; then
  test -n "$upstream_url"
  test -n "$cache_put_url"
  curl --fail-with-body --show-error --location "$upstream_url" -o "$archive"
  {_expected_sha256(binary, version)}
  echo "$expected  $archive" | sha256sum -c -
  s3_curl - -H 'Content-Type: application/octet-stream' --upload-file "$archive" "$cache_put_url"
fi
{extract}
installed="$extract_dir/{binary}"
if [ ! -f "$installed" ]; then installed="$extract_dir/{binary}-linux-amd64"; fi
test -f "$installed"
mv "$installed" "$BIN_DIR/{binary}"
rm -rf "$extract_dir"
chmod +x "$BIN_DIR/{binary}"
'''
