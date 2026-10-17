# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from src.domain.cmd_builder.installers import (
    PINNED_UPSTREAM_URLS,
    cache_key,
    env_suffix,
    render_installer,
    require_pinned_installer,
    require_runtime,
    runtime_release,
)
from src.domain.cmd_builder.script_generator import S3_CURL_HELPER

# One version of each runtime outside the list openci-tf used to pin.
RUNTIMES = (("tofu", "1.11.14"), ("terraform", "1.11.4"))


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
curl_args=" $* "
ok() {{ case "$curl_args" in *" -w "*) printf 200 ;; esac; }}
is_cache=false
[[ "$*" == *"cache"* ]] && is_cache=true
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    if $is_cache && [ {cache_exit} -ne 0 ]; then exit {cache_exit}; fi
    cp "{archive}" "$2"; ok; exit 0
  fi
  shift
done
''')
    curl.chmod(0o755)


def test_installer_version_selects_a_versioned_archive():
    old_version = render_installer("tofu", "1.10.6", "lambda")
    new_version = render_installer("tofu", "1.12.6", "lambda")

    assert 'archive="$BIN_DIR/tofu-1.10.6.download"' in old_version
    assert 'archive="$BIN_DIR/tofu-1.12.6.download"' in new_version
    assert "UPSTREAM_URL_TOFU_1_10_6" in old_version
    assert "UPSTREAM_URL_TOFU_1_12_6" in new_version
    assert old_version != new_version
    assert 'curl --fail-with-body --show-error --location "$upstream_url" -o "$archive"' in old_version
    assert 'if ! s3_curl "$archive" "$cache_get_url"; then' in old_version
    assert 's3_curl - -H \'Content-Type: application/octet-stream\' --upload-file "$archive" "$cache_put_url"' in old_version
    put_lines = [line for line in old_version.splitlines() if '--upload-file "$archive"' in line and "cache_put_url" in line]
    assert put_lines
    assert all("--location" not in line for line in put_lines)


def test_runtime_downloads_name_the_release_archive_and_its_checksum_file():
    expected = {
        ("tofu", "1.10.6"): "https://github.com/opentofu/opentofu/releases/download/v1.10.6/tofu_1.10.6_linux_amd64.tar.gz",
        ("tofu", "1.11.14"): "https://github.com/opentofu/opentofu/releases/download/v1.11.14/tofu_1.11.14_linux_amd64.tar.gz",
        ("terraform", "1.10.5"): "https://releases.hashicorp.com/terraform/1.10.5/terraform_1.10.5_linux_amd64.zip",
        ("terraform", "1.11.4"): "https://releases.hashicorp.com/terraform/1.11.4/terraform_1.11.4_linux_amd64.zip",
    }

    for (binary, version), url in expected.items():
        release = runtime_release(binary, version)
        directory, archive = url.rsplit("/", 1)
        assert release.url == url
        assert release.archive == archive
        assert release.sums_url == f"{directory}/{binary}_{version}_SHA256SUMS"
        assert cache_key(binary, version) == f"cache/{binary}/{version}"
        assert env_suffix(binary, version) == f"{binary}_{version}".replace(".", "_").upper()


def test_only_the_scan_tools_stay_pinned():
    assert set(PINNED_UPSTREAM_URLS) == {"tfsec:1.28.10", "infracost:0.10.39"}


def test_unpinned_installer_is_rejected_clearly():
    with pytest.raises(ValueError, match="unsupported unpinned installer tfsec:9.9.9"):
        require_pinned_installer("tfsec", "9.9.9")


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        ("tofu:latest", "binary:version"),
        ("tofu:1.11", "binary:version"),
        ("tfsec:1.28.10", "binary:version"),
        ("terraform:1.9.8", "predates S3 native state locking"),
    ],
)
def test_malformed_or_pre_lock_file_runtime_is_refused(runtime, message):
    with pytest.raises(ValueError, match=message):
        require_runtime(runtime)


def test_installer_executes_cache_hit_path(tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    _curl(tmp_path, archive, 0)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("CACHE_PUT_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("UPSTREAM_URL_TOFU", "https://upstream/tofu")
    subprocess.run(["bash", "-c", "set -euo pipefail\n" + S3_CURL_HELPER + "\n" + render_installer("tofu", "1.10.6", "lambda")], check=True)
    assert Path("/tmp/lambda/bin/tofu").exists()


def test_installer_fails_on_a_scan_tool_sha256_mismatch(tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    _curl(tmp_path, archive, 22)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TFSEC", "https://cache/tfsec")
    monkeypatch.setenv("CACHE_PUT_URL_TFSEC", "https://cache/tfsec")
    monkeypatch.setenv("UPSTREAM_URL_TFSEC", "https://upstream/tfsec")
    completed = subprocess.run(["bash", "-c", "set -euo pipefail\n" + S3_CURL_HELPER + "\n" + render_installer("tfsec", "1.28.10", "lambda")], text=True, capture_output=True, check=False)
    assert completed.returncode != 0
    assert "FAILED" in completed.stdout


def _runtime_archive(tmp_path: Path, binary: str) -> Path:
    """The release archive shape: tofu ships a .tar.gz, terraform a .zip."""
    source = tmp_path / binary
    source.write_text(f"#!/usr/bin/env bash\necho {binary}\n")
    source.chmod(0o755)
    if binary == "terraform":
        archive = tmp_path / "terraform.zip"
        with zipfile.ZipFile(archive, "w") as contents:
            contents.write(source, arcname=binary)
    else:
        archive = tmp_path / "tofu.tar.gz"
        with tarfile.open(archive, "w:gz") as contents:
            contents.add(source, arcname=binary)
    return archive


def _run_runtime_install(tmp_path: Path, monkeypatch, binary: str, version: str, sums: str):
    """Cache GET misses with 403; the upstream serves the release archive; the
    release's SHA256SUMS file is ``sums``: a matching entry, a wrong hash, no
    entry for this archive, or no file at all (404)."""
    archive = _runtime_archive(tmp_path, binary)
    release = runtime_release(binary, version)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sums_file = tmp_path / "SHA256SUMS"
    sums_file.write_text(
        {
            "match": f"{digest}  {release.archive}\n",
            "mismatch": f"{'0' * 64}  {release.archive}\n",
            # A sibling archive and a name that only starts with this one.
            "missing_entry": f"{digest}  {release.archive.replace('linux_amd64', 'darwin_arm64')}\n"
            f"{digest}  {release.archive}.sig\n",
            "missing_file": "",
        }[sums]
    )
    sums_answer = (
        'echo "curl: (22) The requested URL returned error: 404" >&2; exit 22'
        if sums == "missing_file"
        else f'cp "{sums_file}" "$out"'
    )
    calls = tmp_path / "curl-calls.log"
    curl = tmp_path / "curl"
    curl.write_text(f'''#!/usr/bin/env bash
out=""; url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w|-H|--upload-file) shift ;;
    https://*) url="$1" ;;
  esac
  shift
done
echo "$url" >> "{calls}"
case "$url" in
  https://cache-put/*) : > "$out"; printf 200 ;;
  https://cache/*) printf '%s' '<Error><Code>AccessDenied</Code></Error>' > "$out"; printf 403 ;;
  https://upstream/*) cp "{archive}" "$out" ;;
  {release.sums_url}) {sums_answer} ;;
  *) exit 99 ;;
esac
''')
    curl.chmod(0o755)
    name = env_suffix(binary, version)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv(f"CACHE_GET_URL_{name}", f"https://cache/{binary}")
    monkeypatch.setenv(f"CACHE_PUT_URL_{name}", f"https://cache-put/{binary}")
    monkeypatch.setenv(f"UPSTREAM_URL_{name}", f"https://upstream/{binary}")
    script = "set -euo pipefail\n" + S3_CURL_HELPER + "\n" + render_installer(binary, version, "lambda")
    completed = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)
    return completed, calls.read_text().splitlines(), release


@pytest.mark.parametrize(("binary", "version"), RUNTIMES)
def test_runtime_installs_once_its_release_checksum_file_matches(tmp_path, monkeypatch, binary, version):
    completed, calls, release = _run_runtime_install(tmp_path, monkeypatch, binary, version, "match")

    assert completed.returncode == 0, completed.stderr
    assert calls == [
        f"https://cache/{binary}",
        f"https://upstream/{binary}",
        release.sums_url,
        f"https://cache-put/{binary}",
    ]
    installed = subprocess.run([f"/tmp/lambda/bin/{binary}"], text=True, capture_output=True, check=True)
    assert installed.stdout.strip() == binary


@pytest.mark.parametrize(("binary", "version"), RUNTIMES)
@pytest.mark.parametrize(
    ("sums", "returncode", "message"),
    [
        ("missing_file", 22, "returned error: 404"),
        ("missing_entry", 1, "lists no"),
        ("mismatch", 1, "FAILED"),
    ],
)
def test_runtime_install_fails_loud_without_a_matching_checksum(
    tmp_path, monkeypatch, binary, version, sums, returncode, message
):
    completed, calls, _release = _run_runtime_install(tmp_path, monkeypatch, binary, version, sums)

    assert completed.returncode == returncode
    assert message in completed.stdout + completed.stderr
    assert not [call for call in calls if call.startswith("https://cache-put/")], "an unverified archive is never cached"


# The installer script's sleep advances bash's SECONDS instead of waiting, so the
# s3_curl retry window runs instantly.
_SLEEP_ADVANCES_CLOCK = 'sleep() { SECONDS=$((SECONDS + $1)); }\n'
_SLOWDOWN = "<Error><Code>SlowDown</Code><Message>Please reduce your request rate.</Message></Error>"
_TOFU_SUMS_URL = "https://github.com/opentofu/opentofu/releases/download/v1.10.6/tofu_1.10.6_SHA256SUMS"


def _run_installer_against_throttled_cache(tmp_path: Path, monkeypatch, *, put_failures: int):
    """Cache GET misses with 403; the cache PUT answers 503 SlowDown put_failures times, then 200."""
    archive = _archive(tmp_path)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  tofu_1.10.6_linux_amd64.tar.gz\n")
    calls = tmp_path / "curl-calls.log"
    curl = tmp_path / "curl"
    curl.write_text(f'''#!/usr/bin/env bash
out=""; url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w|-H|--upload-file) shift ;;
    https://*) url="$1" ;;
  esac
  shift
done
echo "$url" >> "{calls}"
case "$url" in
  https://cache-put/*)
    if [ "$(grep -c '^https://cache-put/' "{calls}")" -le {put_failures} ]; then
      printf '%s' '{_SLOWDOWN}' > "$out"; printf 503; exit 0
    fi
    : > "$out"; printf 200 ;;
  https://cache/*) printf '%s' '<Error><Code>AccessDenied</Code></Error>' > "$out"; printf 403 ;;
  https://upstream/*) cp "{archive}" "$out" ;;
  {_TOFU_SUMS_URL}) cp "{sums}" "$out" ;;
  *) exit 99 ;;
esac
''')
    curl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("CACHE_PUT_URL_TOFU", "https://cache-put/tofu")
    monkeypatch.setenv("UPSTREAM_URL_TOFU", "https://upstream/tofu")
    script = "set -euo pipefail\n" + _SLEEP_ADVANCES_CLOCK + S3_CURL_HELPER + "\n" + render_installer("tofu", "1.10.6", "lambda")
    completed = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)
    return completed, calls.read_text().splitlines()


def test_installer_cache_put_retries_slowdown_and_succeeds_on_third_attempt(tmp_path, monkeypatch):
    completed, calls = _run_installer_against_throttled_cache(tmp_path, monkeypatch, put_failures=2)

    assert completed.returncode == 0, completed.stderr
    assert calls == [
        "https://cache/tofu",
        "https://upstream/tofu",
        _TOFU_SUMS_URL,
        "https://cache-put/tofu",
        "https://cache-put/tofu",
        "https://cache-put/tofu",
    ]
    assert completed.stderr.count("Warning: S3 request returned HTTP 503") == 2
    assert Path("/tmp/lambda/bin/tofu").exists()


def test_installer_cache_miss_403_is_not_retried(tmp_path, monkeypatch):
    completed, calls = _run_installer_against_throttled_cache(tmp_path, monkeypatch, put_failures=0)

    assert completed.returncode == 0, completed.stderr
    assert calls.count("https://cache/tofu") == 1
    assert "curl: (22) The requested URL returned error: 403" in completed.stderr


def test_installer_cache_put_fails_loud_after_at_least_sixty_seconds_of_slowdown(tmp_path, monkeypatch):
    completed, calls = _run_installer_against_throttled_cache(tmp_path, monkeypatch, put_failures=1_000)

    assert completed.returncode == 22
    assert _SLOWDOWN in completed.stderr
    final = next(line for line in completed.stderr.splitlines() if line.startswith("Error: S3 request still failing"))
    assert "HTTP 503 SlowDown" in final
    waited = int(final.rsplit(" over ", 1)[1].removesuffix("s"))
    assert waited >= 60
    assert 3 < calls.count("https://cache-put/tofu") < 20


def test_s3_curl_redirect_is_failure_and_dest_is_not_written(tmp_path, monkeypatch):
    redirect = "<Error><Code>TemporaryRedirect</Code><Message>Please re-send this request to the specified temporary endpoint.</Message></Error>"
    calls = tmp_path / "curl-calls.log"
    curl = tmp_path / "curl"
    curl.write_text(f"""#!/usr/bin/env bash
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) shift ;;
    https://*) echo "$1" >> "{calls}" ;;
  esac
  shift
done
printf '%s' '{redirect}' > "$out"; printf 307
""")
    curl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    dest = tmp_path / "plan.tfplan"
    script = "set -euo pipefail\n" + _SLEEP_ADVANCES_CLOCK + S3_CURL_HELPER + f'\ns3_curl "{dest}" https://bucket.s3.amazonaws.com/plan.tfplan\n'

    completed = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    assert completed.returncode == 22
    assert calls.read_text().splitlines() == ["https://bucket.s3.amazonaws.com/plan.tfplan"]
    assert "curl: (22) The requested URL returned error: 307" in completed.stderr
    assert redirect in completed.stderr
    assert not dest.exists()
