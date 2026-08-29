# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.errors import ConfigResolutionError
from src.core.models import FolderConfig
from src.domain.cmd_builder.cmd_resolver import resolve_commands
from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.config.outer_state import resolve_outer_state
from src.platform.aws.sops import encrypt_file


def _upload_content_type_case_lines(script: str) -> list[str]:
    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == 'case "$name" in')
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "esac")
    return [line.strip() for line in lines[start : end + 1]]


def _simulate_upload_content_type(case_lines: list[str], artifact_name: str) -> str:
    for line in case_lines:
        if line.startswith("*.out)"):
            if artifact_name.endswith(".out") and not artifact_name.endswith(".output"):
                return "text/plain"
        elif line.startswith("*.output)"):
            if artifact_name.endswith(".output"):
                return "text/plain"
        elif line.startswith("*.json)"):
            if artifact_name.endswith(".json"):
                return "application/json"
        elif line.startswith("*)"):
            return "application/octet-stream"
    raise AssertionError(f"no upload content-type case matched {artifact_name!r}")


@pytest.mark.parametrize("verb", ["plan", "report"])
def test_native_output_artifacts_upload_with_text_plain_content_type(verb):
    """Regression: presigned tfsec.output/infracost.output require text/plain."""
    script = render(ScriptParams(verb=verb, execution_target="lambda"))
    case_lines = _upload_content_type_case_lines(script)
    assert any(line.startswith("*.output)") and "text/plain" in line for line in case_lines)
    for artifact in ("tfsec.output", "infracost.output"):
        assert _simulate_upload_content_type(case_lines, artifact) == "text/plain", artifact
    assert _simulate_upload_content_type(case_lines, "tfsec.json") == "application/json"
    assert _simulate_upload_content_type(case_lines, "tf/plan.out") == "text/plain"


@pytest.mark.parametrize("verb", ["apply", "destroy"])
def test_mutation_upload_helpers_map_output_suffix_to_text_plain(verb):
    script = render(ScriptParams(verb=verb, execution_target="lambda"))
    case_lines = _upload_content_type_case_lines(script)
    assert any(line.startswith("*.output)") and "text/plain" in line for line in case_lines)


@pytest.mark.parametrize("verb", ["plan", "drift", "report"])
def test_script_is_generated_for_each_safe_verb(verb):
    script = render(ScriptParams(verb=verb, execution_target="lambda", folder="folder with spaces"))
    assert "set -euo pipefail" in script
    assert "trap _on_exit EXIT" in script
    assert "upload_artifacts" in script
    assert "curl -sS --fail-with-body --retry 3" in script
    assert "cd 'folder with spaces'" in script


def test_drift_exit_two_is_normalized_and_adversarial_flags_are_quoted_through_resolver():
    resolved = resolve_commands("drift", FolderConfig(account_alias="target", execution_target="codebuild", extra_flags=("-var=x; rm -rf /", "$(bad)")))
    script = render(ScriptParams(verb=resolved.verb, execution_target=resolved.execution_target, normalize_drift=resolved.normalize_drift, extra_flags=resolved.extra_flags))
    assert 'if [ true = true ]; then' in script
    assert '[ "$status" -eq 2 ]' in script
    assert '[ "$status" -eq 0 ]' in script
    assert "export PATH=/usr/local/bin:$PATH" in script
    assert "'$(bad)'" in script
    assert "'-var=x; rm -rf /'" in script


def test_drift_script_is_runtime_only_without_tfsec_or_infracost():
    script = render(ScriptParams(verb="drift", execution_target="lambda"))
    assert "tofu init -no-color" in script
    assert "tofu validate -no-color" in script
    assert "tofu plan -no-color -detailed-exitcode" in script
    assert "tfsec" not in script.lower()
    assert "infracost" not in script.lower()


@pytest.mark.parametrize("verb", ["plan", "report"])
def test_plan_and_report_scripts_still_install_and_execute_shared_tools(verb):
    script = render(ScriptParams(verb=verb, execution_target="lambda"))
    assert "UPSTREAM_URL_TFSEC_1_28_10" in script
    assert "UPSTREAM_URL_INFRACOST_0_10_39" in script
    assert 'curl --fail-with-body --show-error --location "$upstream_url" -o "$archive"' in script
    assert 'curl --fail-with-body --show-error -H \'Content-Type: application/octet-stream\' --upload-file "$archive" "$cache_put_url"' in script
    put_lines = [line for line in script.splitlines() if '--upload-file "$archive"' in line and "cache_put_url" in line]
    assert put_lines
    assert all("--location" not in line for line in put_lines)
    assert "tfsec . --format json --soft-fail --out" in script
    assert "infracost breakdown --path . --format json --out-file" in script


def _write_folder_config(root: Path, runtime: str) -> None:
    config_dir = root / "infra/app/.openci_tf"
    config_dir.mkdir(parents=True)
    config_dir.joinpath("config.yaml").write_text(
        f"version: 1\ntf_runtime: {runtime}\naccount_alias: target\n"
    )


@pytest.mark.parametrize(
    ("runtime", "key"),
    [("tofu:1.8.0", "tofu:1.8.0"), ("terraform:1.8.5", "terraform:1.8.5")],
)
def test_drift_outer_state_requires_only_configured_runtime_url(tmp_path, runtime, key):
    _write_folder_config(tmp_path, runtime)
    result = resolve_outer_state(
        str(tmp_path),
        ["infra/app"],
        {key: f"https://downloads.example/{key}"},
        "drift",
    )
    assert result["upstream_urls"] == {key: f"https://downloads.example/{key}"}


def test_outer_state_allows_legacy_single_runtime_url_key(tmp_path):
    _write_folder_config(tmp_path, "tofu:1.8.0")
    result = resolve_outer_state(str(tmp_path), ["infra/app"], {"tofu": "https://downloads.example/tofu"}, "drift")
    assert result["upstream_urls"] == {"tofu:1.8.0": "https://downloads.example/tofu"}


def test_outer_state_rejects_unpinned_runtime_clearly(tmp_path):
    _write_folder_config(tmp_path, "terraform:1.10.0")
    with pytest.raises(ConfigResolutionError, match="unsupported unpinned tf_runtime terraform:1.10.0"):
        resolve_outer_state(
            str(tmp_path),
            ["infra/app"],
            {"terraform:1.10.0": "https://downloads.example/terraform"},
            "drift",
        )


def test_outer_state_resolves_mixed_terraform_and_tofu_versions(tmp_path):
    folders = []
    for folder, runtime in {
        "infra/tf185": "terraform:1.8.5",
        "infra/tf198": "terraform:1.9.8",
        "infra/tofu180": "tofu:1.8.0",
        "infra/tofu190": "tofu:1.9.0",
    }.items():
        config_dir = tmp_path / folder / ".openci_tf"
        config_dir.mkdir(parents=True)
        config_dir.joinpath("config.yaml").write_text(
            f"version: 1\ntf_runtime: {runtime}\naccount_alias: target\n"
        )
        folders.append(folder)

    upstream_urls = {runtime: f"https://downloads.example/{runtime}" for runtime in (
        "terraform:1.8.5",
        "terraform:1.9.8",
        "tofu:1.8.0",
        "tofu:1.9.0",
    )}
    result = resolve_outer_state(str(tmp_path), folders, upstream_urls, "drift")

    assert result["upstream_urls"] == upstream_urls


@pytest.mark.parametrize("action", ["plan", "report"])
def test_plan_and_report_outer_state_still_require_shared_installer_urls(tmp_path, action):
    _write_folder_config(tmp_path, "tofu:1.8.0")
    with pytest.raises(ConfigResolutionError, match="infracost:0.10.39"):
        resolve_outer_state(str(tmp_path), ["infra/app"], {"tofu:1.8.0": "https://downloads.example/tofu"}, action)

    result = resolve_outer_state(
        str(tmp_path),
        ["infra/app"],
        {
            "tofu:1.8.0": "https://downloads.example/tofu",
            "tfsec:1.28.10": "https://downloads.example/tfsec",
            "infracost:0.10.39": "https://downloads.example/infracost",
        },
        action,
    )
    assert set(result["upstream_urls"]) == {"tofu:1.8.0", "tfsec:1.28.10", "infracost:0.10.39"}


def test_sops_uses_minimal_environment_and_wipes_plaintext(tmp_path, monkeypatch):
    plaintext = tmp_path / "secrets.json"
    plaintext.write_text("secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "lambda-identity")
    captured = {}

    def runner(command, **kwargs):
        captured.update(kwargs)
        Path(command[3]).write_text("encrypted")
        return SimpleNamespace(returncode=0, stderr="")

    encrypted = encrypt_file(str(plaintext), "arn:aws:kms:region:account:key/id", runner)
    assert Path(encrypted).read_text() == "encrypted"
    assert captured["env"]["AWS_ACCESS_KEY_ID"] == "lambda-identity"
    assert captured["env"]["SOPS_KMS_ARN"].endswith("key/id")
    assert not plaintext.exists()
def test_sops_wipes_plaintext_when_runner_fails(tmp_path):
    plaintext = tmp_path / "secrets.json"
    plaintext.write_text("secret")
    with pytest.raises(RuntimeError, match="sops encryption failed"):
        encrypt_file(str(plaintext), "key", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="no"))
    assert not plaintext.exists()
