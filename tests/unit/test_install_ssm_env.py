"""Tests for the SSM dotenv installer script."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALL = _REPO_ROOT / "scripts" / "install_ssm_env.sh"
_VALID_PATH = "/openci-tf/env/github/example-org/private-module-repo"


def _run(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(_REPO_ROOT)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(script), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged,
        cwd=_REPO_ROOT,
    )


def test_install_rejects_invalid_path_and_malformed_dotenv(tmp_path):
    dotenv = tmp_path / "bad.env"
    dotenv.write_text("AWS_SECRET=evil\n")
    completed = _run(_INSTALL, _VALID_PATH, str(dotenv))
    assert completed.returncode != 0
    assert "protected" in completed.stderr.lower() or "protected" in completed.stdout.lower()

    completed = _run(_INSTALL, "/openci-tf/install/not-env", str(tmp_path / "missing.env"))
    assert completed.returncode != 0


def test_install_does_not_echo_secret_contents(tmp_path, monkeypatch):
    dotenv = tmp_path / "github.env"
    dotenv.write_text("GITHUB_TOKEN=FAKE_SENTINEL_TOKEN_VALUE\n")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    aws = tmp_path / "aws"
    aws.write_text("#!/usr/bin/env bash\nexit 0\n")
    aws.chmod(0o755)

    completed = _run(_INSTALL, _VALID_PATH, str(dotenv), env={"PATH": f"{tmp_path}:{os.environ['PATH']}"})
    assert completed.returncode == 0
    assert "FAKE_SENTINEL_TOKEN_VALUE" not in completed.stdout
    assert "FAKE_SENTINEL_TOKEN_VALUE" not in completed.stderr
