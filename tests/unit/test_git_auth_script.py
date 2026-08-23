"""Tests for token-free Git authentication in the generated runner script."""

from __future__ import annotations

import os
import stat
import subprocess

from src.domain.cmd_builder.script_generator import ScriptParams, render

_SENTINEL = "FAKE_SENTINEL_TOKEN_VALUE"


def _render() -> str:
    return render(ScriptParams(verb="plan", execution_target="lambda", folder="infra/vpc"))


def test_generated_script_contains_rewrite_and_askpass_without_sentinel():
    script = _render()
    assert "export GIT_EXEC_PATH=/opt/bin/libexec/git-core" in script
    assert "GIT_CONFIG_COUNT=2" in script
    assert "GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf" in script
    assert "GIT_CONFIG_VALUE_0=git@github.com:" in script
    assert "GIT_CONFIG_KEY_1=url.https://github.com/.insteadOf" in script
    assert "GIT_CONFIG_VALUE_1=ssh://git@github.com/" in script
    assert "GIT_TERMINAL_PROMPT=0" in script
    assert "OPENCI_TF_ASKPASS_EOF" in script
    assert "x-access-token" in script
    assert 'printf \'%s\' "${GITHUB_TOKEN}"' in script
    assert _SENTINEL not in script


def test_git_auth_is_conditional_on_github_token():
    script = _render()
    assert 'if [ -n "${GITHUB_TOKEN:-}" ]; then' in script
    assert 'rm -f "${_OPENCI_TF_GIT_ASKPASS}"' in script
    assert "_on_exit" in script


def test_askpass_helper_prompt_sequence_reads_token_from_environment(tmp_path):
    script = _render()
    start = script.index("<<'OPENCI_TF_ASKPASS_EOF'") + len("<<'OPENCI_TF_ASKPASS_EOF'\n")
    end = script.index("OPENCI_TF_ASKPASS_EOF", start)
    helper = tmp_path / "askpass"
    helper.write_text(script[start:end])
    helper.chmod(stat.S_IMODE(helper.stat().st_mode) | stat.S_IXUSR)
    env = {**os.environ, "GITHUB_TOKEN": _SENTINEL}
    user = subprocess.run([str(helper), "Username for 'https://github.com':"], capture_output=True, text=True, env=env, check=True)
    password = subprocess.run([str(helper), "Password for 'https://github.com':"], capture_output=True, text=True, env=env, check=True)
    assert user.stdout == "x-access-token"
    assert password.stdout == _SENTINEL
