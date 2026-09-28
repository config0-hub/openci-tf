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
from src.domain.cmd_builder.script_generator import S3_CURL_HELPER


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
    old_version = render_installer("tofu", "1.10.6", "lambda", "0" * 64)
    new_version = render_installer("tofu", "1.12.6", "lambda", "0" * 64)

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
    subprocess.run(["bash", "-c", "set -euo pipefail\n" + S3_CURL_HELPER + "\n" + render_installer("tofu", "1.10.6", "lambda", "0" * 64)], check=True)
    assert Path("/tmp/lambda/bin/tofu").exists()


def test_installer_fails_on_real_sha256_mismatch(tmp_path, monkeypatch):
    archive = _archive(tmp_path)
    _curl(tmp_path, archive, 22)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("CACHE_PUT_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("UPSTREAM_URL_TOFU", "https://upstream/tofu")
    completed = subprocess.run(["bash", "-c", "set -euo pipefail\n" + S3_CURL_HELPER + "\n" + render_installer("tofu", "1.10.6", "lambda", "0" * 64)], text=True, capture_output=True, check=False)
    assert completed.returncode != 0
    assert "FAILED" in completed.stdout


# The installer script's sleep advances bash's SECONDS instead of waiting, so the
# s3_curl retry window runs instantly.
_SLEEP_ADVANCES_CLOCK = 'sleep() { SECONDS=$((SECONDS + $1)); }\n'
_SLOWDOWN = "<Error><Code>SlowDown</Code><Message>Please reduce your request rate.</Message></Error>"


def _run_installer_against_throttled_cache(tmp_path: Path, monkeypatch, *, put_failures: int):
    """Cache GET misses with 403; the cache PUT answers 503 SlowDown put_failures times, then 200."""
    archive = _archive(tmp_path)
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
  *) exit 99 ;;
esac
''')
    curl.chmod(0o755)
    checksum = __import__("hashlib").sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CACHE_GET_URL_TOFU", "https://cache/tofu")
    monkeypatch.setenv("CACHE_PUT_URL_TOFU", "https://cache-put/tofu")
    monkeypatch.setenv("UPSTREAM_URL_TOFU", "https://upstream/tofu")
    script = "set -euo pipefail\n" + _SLEEP_ADVANCES_CLOCK + S3_CURL_HELPER + "\n" + render_installer("tofu", "1.10.6", "lambda", checksum)
    completed = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)
    return completed, calls.read_text().splitlines()


def test_installer_cache_put_retries_slowdown_and_succeeds_on_third_attempt(tmp_path, monkeypatch):
    completed, calls = _run_installer_against_throttled_cache(tmp_path, monkeypatch, put_failures=2)

    assert completed.returncode == 0, completed.stderr
    assert calls == [
        "https://cache/tofu",
        "https://upstream/tofu",
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
