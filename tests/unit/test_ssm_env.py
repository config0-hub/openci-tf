"""Tests for hub SSM dotenv path validation, parsing, and resolution."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.errors import ConfigValidationError, SsmEnvError
from src.domain.config.folder_config import parse_folder_config
from src.domain.engine.payload import EnginePayload
from src.domain.engine.prepare import prepare_and_submit
from src.domain.ssm_env.dotenv import is_protected_env_name, parse_dotenv
from src.domain.ssm_env.paths import (
    SSM_ENV_PREFIX,
    validate_ssm_env_path,
    validate_ssm_env_paths,
)
from src.domain.ssm_env.resolve import resolve_ssm_env_vars
from src.platform.git.package import build_package
from src.services.run_folder import prepare_and_submit as prepare_handler

_SENTINEL = "FAKE_SENTINEL_TOKEN_VALUE"
_VALID_PATH = "/openci-tf/env/github/example-org/private-module-repo"


def _mock_prepare_hub_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare_handler.sts, "get_caller_account_id", lambda: "REPLACE_MAIN_ACCOUNT")


def test_folder_config_defaults_ssm_env_paths_empty():
    config = parse_folder_config("account_alias: target\n")
    assert config.ssm_env_paths == ()


def test_folder_config_accepts_ssm_env_paths_list():
    config = parse_folder_config(
        "account_alias: target\n"
        "ssm_env_paths:\n"
        f"  - {_VALID_PATH}\n"
    )
    assert config.ssm_env_paths == (_VALID_PATH,)


def test_folder_config_rejects_scalar_ssm_env_paths():
    with pytest.raises(ConfigValidationError, match="list"):
        parse_folder_config(f"account_alias: target\nssm_env_paths: {_VALID_PATH}\n")


@pytest.mark.parametrize(
    "path,message",
    [
        ("/openci-tf/install/foo", "begin with"),
        ("/openci-tf/env/", "malformed"),
        ("/openci-tf/env/*/github", "wildcards"),
        ("/openci-tf/env/github/../escape", "malformed"),
        ("/openci-tf/env/github/foo/", "malformed"),
        ("/openci-tf/env/github//foo", "malformed"),
        ("  /openci-tf/env/github/foo", "malformed"),
        ("", "non-empty"),
    ],
)
def test_ssm_env_path_validation_rejects_invalid(path, message):
    with pytest.raises(ConfigValidationError, match=message):
        validate_ssm_env_path(path)


def test_ssm_env_paths_reject_duplicates_and_limits():
    with pytest.raises(ConfigValidationError, match="duplicate"):
        validate_ssm_env_paths([_VALID_PATH, _VALID_PATH])
    with pytest.raises(ConfigValidationError, match="limit"):
        validate_ssm_env_paths([f"{SSM_ENV_PREFIX}segment-{index}" for index in range(5)])


def test_dotenv_parses_comments_export_quotes_and_first_equals():
    parsed = parse_dotenv(
        "# comment\n"
        "export GITHUB_TOKEN=alpha\n"
        'OTHER="value=with=equals"\n'
        "QUOTED='single'\n"
    )
    assert parsed == {
        "GITHUB_TOKEN": "alpha",
        "OTHER": "value=with=equals",
        "QUOTED": "single",
    }


def test_dotenv_rejects_malformed_duplicate_protected_and_nul():
    with pytest.raises(SsmEnvError, match="malformed"):
        parse_dotenv("NOT_A_VAR\nGITHUB_TOKEN=x\n")
    with pytest.raises(SsmEnvError, match="duplicate"):
        parse_dotenv("GITHUB_TOKEN=one\nGITHUB_TOKEN=two\n")
    with pytest.raises(SsmEnvError, match="protected"):
        parse_dotenv("AWS_ACCESS_KEY_ID=evil\n")
    with pytest.raises(SsmEnvError, match="protected"):
        parse_dotenv("INFRACOST_API_KEY=evil\n")
    with pytest.raises(SsmEnvError, match="NUL"):
        parse_dotenv("GITHUB_TOKEN=bad\x00\n")


def test_dotenv_rejects_mismatched_quotes():
    with pytest.raises(SsmEnvError, match="mismatched quotes"):
        parse_dotenv('GITHUB_TOKEN="unclosed\n')
    with pytest.raises(SsmEnvError, match="mismatched quotes"):
        parse_dotenv("GITHUB_TOKEN='unclosed\n")


def test_dotenv_rejects_oversized_value_and_count():
    with pytest.raises(SsmEnvError, match="4096"):
        parse_dotenv(f"GITHUB_TOKEN={'x' * 4097}\n")
    body = "\n".join(f"VAR_{index}=x" for index in range(65))
    with pytest.raises(SsmEnvError, match="64 variables"):
        parse_dotenv(body)


def test_protected_names_include_git_controls_but_allow_github_token():
    assert is_protected_env_name("GIT_ASKPASS")
    assert is_protected_env_name("GIT_CONFIG_COUNT")
    assert is_protected_env_name("SSH_ASKPASS")
    assert not is_protected_env_name("GITHUB_TOKEN")


def test_resolve_uses_decryption_and_merges_without_overwriting_existing():
    fetched: list[tuple[str, bool]] = []

    def fetch(path: str) -> str:
        fetched.append((path, True))
        if path.endswith("shared"):
            return "GITHUB_TOKEN=one\n"
        return "MODULE_FLAG=true\n"

    merged = resolve_ssm_env_vars(
        (f"{SSM_ENV_PREFIX}shared", f"{SSM_ENV_PREFIX}module"),
        fetch=fetch,
        existing={"ARTIFACTS_DIR": "/tmp/artifacts"},
    )
    assert merged == {"GITHUB_TOKEN": "one", "MODULE_FLAG": "true"}
    assert fetched == [(f"{SSM_ENV_PREFIX}shared", True), (f"{SSM_ENV_PREFIX}module", True)]


def test_resolve_rejects_duplicate_across_parameters_and_existing_collisions():
    with pytest.raises(SsmEnvError, match="across SSM"):
        resolve_ssm_env_vars(
            (_VALID_PATH, f"{SSM_ENV_PREFIX}other"),
            fetch=lambda path: "GITHUB_TOKEN=x\n",
            existing={},
        )
    with pytest.raises(SsmEnvError, match="collides"):
        resolve_ssm_env_vars(
            (_VALID_PATH,),
            fetch=lambda path: "ARTIFACTS_DIR=/evil\n",
            existing={"ARTIFACTS_DIR": "/tmp/artifacts"},
        )


def test_resolve_rejects_merged_count_and_total_value_limits():
    body = "\n".join(f"VAR_{index}=x" for index in range(65))
    with pytest.raises(SsmEnvError, match="64 variables"):
        resolve_ssm_env_vars(
            (_VALID_PATH,),
            fetch=lambda path: body,
            existing={},
        )
    paths = tuple(f"{SSM_ENV_PREFIX}seg-{index}" for index in range(5))
    value_sizes = [3277, 3277, 3277, 3277, 3277]

    def fetch(path: str) -> str:
        index = int(path.rsplit("-", 1)[-1])
        return f"K{index}=" + ("x" * value_sizes[index]) + "\n"

    with pytest.raises(SsmEnvError, match="16384 bytes"):
        resolve_ssm_env_vars(paths, fetch=fetch, existing={})


def test_prepare_handler_fetches_hub_ssm_before_target_assumption(monkeypatch, tmp_path):
    order: list[str] = []
    _mock_prepare_hub_account(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_handler.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(prepare_handler, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(prepare_handler.s3, "presign_get", lambda *args: f"get://{args[1]}")
    monkeypatch.setattr(prepare_handler.s3, "presign_put", lambda *args: f"put://{args[1]}")
    monkeypatch.setattr(prepare_handler.s3, "presign_create_put", lambda *args: f"create-put://{args[1]}")
    monkeypatch.setattr(prepare_handler.sts, "assume_role", lambda *_, **__: order.append("assume") or {"AWS_ACCESS_KEY_ID": "target"})
    monkeypatch.setattr(prepare_handler, "get_github_token", lambda _: "clone-token")
    monkeypatch.setattr(prepare_handler, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(prepare_handler, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_handler.sops, "encrypt_file", lambda path, _: path)
    monkeypatch.setattr(prepare_handler, "build_package", lambda *_args, **_kwargs: str(tmp_path / "package.zip"))
    monkeypatch.setattr(prepare_handler.s3, "upload_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_handler.s3, "head_object", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_handler.engine, "invoke_init_job", lambda *_: None)

    captured: dict[str, str] = {}

    def fake_get_parameter(path: str, with_decryption: bool = True) -> str:
        order.append(f"ssm:{path}:{with_decryption}")
        return f"GITHUB_TOKEN={_SENTINEL}\n"

    monkeypatch.setattr(prepare_handler, "get_parameter", fake_get_parameter)

    def fake_prepare(*, payload, secrets, encrypt, package, upload, submit):
        captured.update(secrets)
        order.append("prepare")
        return {"submitted_at": 1.0}

    monkeypatch.setattr(prepare_handler, "prepare_and_submit", fake_prepare)
    prepare_handler.handler(
        {
            "action": "plan",
            "run_id": "run",
            "folder": "infra/a",
            "budget": 900, "deadline_at": "2999-01-01T00:00:00Z",
            "attempt": 0,
            "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"},
            "folder_config": {"account_alias": "target", "ssm_env_paths": [_VALID_PATH]},
            "git_url": "https://github.com/org/repo.git",
            "commit_hash": "a" * 40,
            "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
            "repo_name": "org/repo",
        },
        object(),
    )
    assert order.index(f"ssm:{_VALID_PATH}:True") < order.index("assume")
    assert captured["GITHUB_TOKEN"] == _SENTINEL
    assert "AWS_ACCESS_KEY_ID" in captured


def test_prepare_handler_rejects_target_credentials_overwriting_ssm_env(monkeypatch, tmp_path):
    _mock_prepare_hub_account(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_handler.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(prepare_handler, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(prepare_handler.s3, "presign_get", lambda *args: f"get://{args[1]}")
    monkeypatch.setattr(prepare_handler.s3, "presign_put", lambda *args: f"put://{args[1]}")
    monkeypatch.setattr(prepare_handler.s3, "presign_create_put", lambda *args: f"create-put://{args[1]}")
    monkeypatch.setattr(
        prepare_handler.sts,
        "assume_role",
        lambda *_, **__: {"GITHUB_TOKEN": "target-overwrite"},
    )
    monkeypatch.setattr(prepare_handler, "get_github_token", lambda _: "clone-token")
    monkeypatch.setattr(prepare_handler, "get_parameter", lambda path, with_decryption=True: f"GITHUB_TOKEN={_SENTINEL}\n")

    with pytest.raises(ValueError, match="target credentials collides"):
        prepare_handler.handler(
            {
                "action": "plan",
                "run_id": "run",
                "folder": "infra/a",
                "budget": 900, "deadline_at": "2999-01-01T00:00:00Z",
                "attempt": 0,
                "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"},
                "folder_config": {"account_alias": "target", "ssm_env_paths": [_VALID_PATH]},
                "git_url": "https://github.com/org/repo.git",
                "commit_hash": "a" * 40,
                "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
                "repo_name": "org/repo",
            },
            object(),
        )


def test_prepare_handler_rejects_infracost_key_from_ssm_env(monkeypatch, tmp_path):
    _mock_prepare_hub_account(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_handler.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(prepare_handler, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(prepare_handler.s3, "presign_get", lambda *args: f"get://{args[1]}")
    monkeypatch.setattr(prepare_handler.s3, "presign_put", lambda *args: f"put://{args[1]}")
    monkeypatch.setattr(prepare_handler.s3, "presign_create_put", lambda *args: f"create-put://{args[1]}")
    monkeypatch.setattr(prepare_handler.sts, "assume_role", lambda *_, **__: {"AWS_ACCESS_KEY_ID": "target"})
    monkeypatch.setattr(prepare_handler, "get_github_token", lambda _: "clone-token")
    monkeypatch.setattr(
        prepare_handler,
        "get_parameter",
        lambda path, with_decryption=True: "INFRACOST_API_KEY=from-ssm\n",
    )

    with pytest.raises(SsmEnvError, match="protected"):
        prepare_handler.handler(
            {
                "action": "plan",
                "run_id": "run",
                "folder": "infra/a",
                "budget": 900, "deadline_at": "2999-01-01T00:00:00Z",
                "attempt": 0,
                "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"},
                "folder_config": {"account_alias": "target", "ssm_env_paths": [_VALID_PATH]},
                "git_url": "https://github.com/org/repo.git",
                "commit_hash": "a" * 40,
                "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
                "repo_name": "org/repo",
            },
            object(),
        )


def test_package_contains_no_plaintext_sentinel_outside_encrypted_secrets(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "main.tf").write_text("terraform {}")
    encrypted = tmp_path / "secrets.enc.json"
    encrypted.write_text(json.dumps({"GITHUB_TOKEN": _SENTINEL}))
    archive = build_package(str(root), str(tmp_path / "package.zip"), "#!/bin/sh", str(encrypted))
    with zipfile.ZipFile(archive) as contents:
        for name in contents.namelist():
            if name == "secrets.enc.json":
                continue
            assert _SENTINEL not in contents.read(name).decode()


def test_engine_payload_contract_unchanged_with_ssm_env_paths():
    import base64
    commands_b64 = base64.b64encode(b'["bash ./openci_tf_run.sh"]').decode()
    payload = EnginePayload(
        "id",
        "s3://bucket/package",
        "kms",
        None,
        commands_b64,
        "s3://bucket/done",
        "lambda",
        900,
    )
    payload.validate()
    assert set(payload.__dict__) == {
        "trigger_id",
        "s3_package_uri",
        "sops_type",
        "sops_path",
        "commands_b64",
        "done_endpoint",
        "execution_target",
        "timeout_seconds",
    }


def test_prepare_and_submit_still_wipes_plaintext_with_ssm_env(tmp_path):
    calls = []

    def encrypt(path):
        calls.append(path)
        encrypted = f"{path}.enc"
        Path(encrypted).write_text("encrypted")
        return encrypted

    prepare_and_submit(
        payload={"trigger_id": "run"},
        secrets={"GITHUB_TOKEN": _SENTINEL},
        encrypt=encrypt,
        package=lambda path: str(tmp_path / "archive.zip"),
        upload=lambda archive: calls.append(archive),
        submit=lambda payload: calls.append("submit"),
    )
    assert not Path(calls[0]).exists()
