# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behavioral acceptance coverage for phase-one execution wiring."""
import json
import os
import subprocess
import tarfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from src.core.errors import (
    CredentialExpiredError,
    EngineAckError,
    SignerHorizonExceededError,
)
from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.engine.presign import effective_horizon, validate_presign_budget
from src.platform.aws import engine, sts
from src.platform.aws.sops import encrypt_file
from src.platform.git.package import build_package
from src.services.run_folder import prepare_and_submit

_CLONE_TOKEN = "/openci-tf/clone-token/test"
_FULL_SHA = "a" * 40
_HUB_ACCOUNT_ID = "REPLACE_MAIN_ACCOUNT"
_DEADLINE_AT = "2999-01-01T00:00:00Z"


def _mock_hub_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare_and_submit.sts, "get_caller_account_id", lambda: _HUB_ACCOUNT_ID)


def test_secrets_enc_json_in_zip_before_upload(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "main.tf").write_text("terraform {}")
    encrypted = tmp_path / "encrypted"
    encrypted.write_text("ciphertext")
    archive = build_package(str(folder), str(tmp_path / "package.zip"), "#!/usr/bin/env bash", str(encrypted))
    with zipfile.ZipFile(archive) as contents:
        assert "secrets.enc.json" in contents.namelist()


def test_presign_horizon_fails_loud_and_tightens_from_credentials():
    credentials = SimpleNamespace(_expiry_time=datetime.now(timezone.utc) + timedelta(seconds=10))
    assert effective_horizon(100, credentials) <= 10
    with pytest.raises(SignerHorizonExceededError):
        validate_presign_budget(11, 10)


def test_platform_sts_passes_external_id_and_ttl(monkeypatch):
    calls = {}
    class Client:
        def assume_role(self, **request):
            calls.update(request)
            return {"Credentials": {"AccessKeyId": "id", "SecretAccessKey": "secret", "SessionToken": "token"}}
    monkeypatch.setattr(sts.boto3, "client", lambda *_args, **_kwargs: Client())
    assert sts.assume_role(
        "arn:aws:iam::123:role/x",
        duration_seconds=900,
        external_id="external",
        policy_json='{"Version":"2012-10-17","Statement":[]}',
    ) == {"AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_SESSION_TOKEN": "token"}
    assert calls["ExternalId"] == "external" and calls["DurationSeconds"] == 900
    assert calls["Policy"] == '{"Version":"2012-10-17","Statement":[]}'


def test_bad_ack_raises_and_short_circuits(monkeypatch):
    class Client:
        def invoke(self, **_kwargs):
            return {"Payload": SimpleNamespace(read=lambda: b'{"status":"bad"}')}
    monkeypatch.setattr(engine.boto3, "client", lambda _: Client())
    with pytest.raises(EngineAckError):
        engine.invoke_init_job("engine", {"trigger_id": "run"})


def test_prepare_handler_presigns_before_minting_credentials_and_submission(monkeypatch, tmp_path):
    _mock_hub_account(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_and_submit.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(prepare_and_submit, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="openci-tf-target", external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    calls = []
    target_credentials = {"AWS_ACCESS_KEY_ID": "target-key-id", "AWS_SECRET_ACCESS_KEY": "target-secret", "AWS_SESSION_TOKEN": "target-session-token"}
    monkeypatch.setattr(prepare_and_submit.sts, "assume_role", lambda *args, **kwargs: calls.append(("assume", kwargs)) or target_credentials)
    monkeypatch.setattr(prepare_and_submit.s3, "presign_get", lambda *args: calls.append(("get", args)) or "get-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_put", lambda *args: calls.append(("put", args)) or "put-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_create_put", lambda *args: calls.append(("put", args)) or "create-put-url")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "lambda-identity")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "lambda-session-token")
    captured = {}

    def runner(command, **kwargs):
        captured["env"] = kwargs["env"]
        Path(command[3]).write_text("encrypted")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(prepare_and_submit.sops, "encrypt_file", lambda plain, kms: encrypt_file(plain, kms, runner))
    monkeypatch.setattr(prepare_and_submit, "build_package", lambda *_: str(tmp_path / "package.zip"))
    monkeypatch.setattr(prepare_and_submit, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(prepare_and_submit, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_and_submit, "get_github_token", lambda _: "github-token")
    monkeypatch.setattr(prepare_and_submit.s3, "upload_file", lambda *_args, **_kwargs: calls.append(("upload",)))
    monkeypatch.setattr(prepare_and_submit.s3, "head_object", lambda *_args, **_kwargs: {"version_id": "baseline", "last_modified": datetime.now(timezone.utc)})
    monkeypatch.setattr(prepare_and_submit.engine, "invoke_init_job", lambda *_: calls.append(("submit",)))
    folder = tmp_path / "folder"; folder.mkdir()
    result = prepare_and_submit.handler({"action": "plan", "run_id": "run", "folder": "infra/app", "budget": 900, "deadline_at": _DEADLINE_AT, "attempt": 0, "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"}, "folder_config": {"account_alias": "target"}, "git_url": "https://github.com/org/repo.git", "commit_hash": _FULL_SHA, "ssm_openci_tf_github_token": _CLONE_TOKEN, "repo_name": "org/repo"}, object())
    assert result["attempt"] == 0
    assert "submitted_at" in result
    assert result["done_baseline_version_id"] == "baseline"
    assert all(value not in captured["env"].values() for value in target_credentials.values())
    assert captured["env"]["AWS_SESSION_TOKEN"] == "lambda-session-token"
    assert calls[-1] == ("submit",)
    assert calls[0][0] == "put"
    assume_index = next(index for index, call in enumerate(calls) if call[0] == "assume")
    assert all(call[0] in {"put", "put", "get"} for call in calls[:assume_index])
    assert calls[assume_index][1]["external_id"] == "openci-tf-6be00970ed31c57d"


def test_prepare_rejects_unpinned_runtime_before_packaging():
    with pytest.raises(ValueError, match="unsupported unpinned tf_runtime terraform:1.10.0"):
        prepare_and_submit._folder_config(
            {"folder_config": {"account_alias": "target", "tf_runtime": "terraform:1.10.0"}}
        )


def test_prepare_runtime_accepts_stored_external_id_derived_from_caller(monkeypatch):
    _mock_hub_account(monkeypatch)
    assert prepare_and_submit._validated_external_id("openci-tf-6be00970ed31c57d", "123456789012") == "openci-tf-6be00970ed31c57d"


def test_prepare_runtime_rejects_stored_external_id_mismatch(monkeypatch):
    _mock_hub_account(monkeypatch)
    with pytest.raises(ValueError, match="external_id does not match"):
        prepare_and_submit._validated_external_id("openci-tf-deadbeefdeadbeef", "123456789012")


def test_prepare_drift_requires_only_runtime_upstream_and_omits_shared_tool_secrets(monkeypatch, tmp_path):
    _mock_hub_account(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_and_submit.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(prepare_and_submit, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(prepare_and_submit.sts, "assume_role", lambda *_args, **_kwargs: {"AWS_ACCESS_KEY_ID": "target"})
    monkeypatch.setattr(prepare_and_submit.s3, "presign_get", lambda *_: "get-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_put", lambda *_: "put-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_create_put", lambda *_: "create-put-url")
    monkeypatch.setattr(prepare_and_submit, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(prepare_and_submit, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_and_submit, "get_github_token", lambda _: "github-token")
    monkeypatch.setattr(prepare_and_submit.s3, "head_object", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("src.services.run_folder.secrets.get_infracost_api_key", lambda _: (_ for _ in ()).throw(AssertionError("drift must not read infracost secret")))
    captured = {}
    monkeypatch.setattr(
        prepare_and_submit,
        "prepare_and_submit",
        lambda **kwargs: captured.update(kwargs) or {"submitted_at": "submitted"},
    )
    folder = tmp_path / "folder"
    folder.mkdir()

    result = prepare_and_submit.handler(
        {
            "action": "drift",
            "run_id": "run",
            "folder": "infra/app",
            "budget": 900,
            "deadline_at": _DEADLINE_AT,
            "attempt": 0,
            "upstream_urls": {"tofu": "https://tofu"},
            "folder_config": {"account_alias": "target"},
            "git_url": "https://github.com/org/repo.git",
            "commit_hash": _FULL_SHA,
            "ssm_openci_tf_github_token": _CLONE_TOKEN,
            "repo_name": "org/repo",
            "ssm_infracost_api_key": "/openci-tf/infracost/api_key",
        },
        object(),
    )

    assert result["submitted_at"] == "submitted"
    secrets = captured["secrets"]
    assert "CACHE_GET_URL_TOFU_1_8_0" in secrets
    assert all("TFSEC" not in key and "INFRACOST" not in key for key in secrets)
    payload = captured["payload"]
    assert payload["execution_target"] == "lambda"


def test_prepare_submitted_at_is_captured_after_upload_before_submit(monkeypatch, tmp_path):
    from src.domain.engine import prepare as prepare_domain

    _mock_hub_account(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_and_submit.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(prepare_and_submit, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(prepare_and_submit.sts, "assume_role", lambda *_args, **_kwargs: {"AWS_ACCESS_KEY_ID": "target"})
    monkeypatch.setattr(prepare_and_submit.s3, "presign_get", lambda *_: "get-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_put", lambda *_: "put-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_create_put", lambda *_: "create-put-url")
    monkeypatch.setattr(prepare_and_submit.sops, "encrypt_file", lambda path, _: path)
    monkeypatch.setattr(prepare_and_submit, "build_package", lambda *_: str(tmp_path / "package.zip"))
    monkeypatch.setattr(prepare_and_submit, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(prepare_and_submit, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_and_submit, "get_github_token", lambda _: "github-token")
    monkeypatch.setattr(prepare_and_submit.s3, "upload_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_and_submit.s3, "head_object", lambda *_args, **_kwargs: {"version_id": "baseline", "last_modified": datetime.now(timezone.utc)})
    clock = iter([100.0, 200.0])
    monkeypatch.setattr(prepare_domain.time, "time", lambda: next(clock))
    submit_times: list[float] = []

    def submit(_payload):
        submit_times.append(prepare_domain.time.time())

    monkeypatch.setattr(prepare_and_submit.engine, "invoke_init_job", lambda *_: submit(None))
    folder = tmp_path / "folder"
    folder.mkdir()
    result = prepare_and_submit.handler(
        {
            "action": "plan",
            "run_id": "run",
            "folder": "infra/app",
            "budget": 900,
            "deadline_at": _DEADLINE_AT,
            "attempt": 0,
            "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"},
            "folder_config": {"account_alias": "target"},
            "git_url": "https://github.com/org/repo.git",
            "commit_hash": _FULL_SHA,
            "ssm_openci_tf_github_token": _CLONE_TOKEN,
            "repo_name": "org/repo",
        },
        object(),
    )
    assert result["submitted_at"] == 100.0
    assert submit_times == [200.0]
    assert result["done_baseline_version_id"] == "baseline"


@pytest.mark.parametrize("failing_adapter, error", [
    ("sops", RuntimeError("ExpiredToken: token expired")),
    ("upload", ClientError({"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "UploadFile")),
    ("submit", ClientError({"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "Invoke")),
])
def test_prepare_handler_classifies_expiry_from_each_preparation_adapter(monkeypatch, tmp_path, failing_adapter, error):
    _mock_hub_account(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_and_submit.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(prepare_and_submit, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(prepare_and_submit.sts, "assume_role", lambda *_args, **_kwargs: {"AWS_ACCESS_KEY_ID": "target"})
    monkeypatch.setattr(prepare_and_submit.s3, "presign_get", lambda *_: "get-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_put", lambda *_: "put-url")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_create_put", lambda *_: "create-put-url")
    monkeypatch.setattr(prepare_and_submit.sops, "encrypt_file", lambda path, _: (_ for _ in ()).throw(error) if failing_adapter == "sops" else path)
    monkeypatch.setattr(prepare_and_submit, "build_package", lambda *_: str(tmp_path / "package.zip"))
    monkeypatch.setattr(prepare_and_submit, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(prepare_and_submit, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_and_submit, "get_github_token", lambda _: "github-token")
    monkeypatch.setattr(prepare_and_submit.s3, "upload_file", lambda *_args, **_kwargs: (_ for _ in ()).throw(error) if failing_adapter == "upload" else None)
    monkeypatch.setattr(prepare_and_submit.s3, "head_object", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_and_submit.engine, "invoke_init_job", lambda *_: (_ for _ in ()).throw(error) if failing_adapter == "submit" else None)
    folder = tmp_path / "folder"
    folder.mkdir()
    event = {"action": "plan", "run_id": "run", "folder": "infra/app", "budget": 900, "deadline_at": _DEADLINE_AT, "attempt": 0, "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"}, "folder_config": {"account_alias": "target"}, "git_url": "https://github.com/org/repo.git", "commit_hash": _FULL_SHA, "ssm_openci_tf_github_token": _CLONE_TOKEN, "repo_name": "org/repo"}
    with pytest.raises(CredentialExpiredError, match="preparation credentials expired"):
        prepare_and_submit.handler(event, object())


def test_generated_script_uploads_artifacts_after_early_command_failure(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for binary in ("tofu", "tfsec", "infracost"):
        source = tmp_path / binary
        body = "mkdir -p \"$ARTIFACTS_DIR\"; echo failure > \"$ARTIFACTS_DIR/failure.txt\"; exit 7" if binary == "tofu" else "exit 0"
        source.write_text(f"#!/usr/bin/env bash\nif [ \"${{1:-}}\" = init ]; then {body}; fi\n")
        source.chmod(0o755)
        with tarfile.open(downloads / f"{binary}.tar.gz", "w:gz") as archive:
            archive.add(source, arcname=binary)
    uploads = tmp_path / "uploads.log"
    curl = tmp_path / "curl"
    curl.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" --upload-file "* ]]; then echo "$*" >> "{uploads}"; exit 0; fi
for arg in "$@"; do case "$arg" in https://upstream/*) source="$arg" ;; esac; done
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then cp "{downloads}/$(basename "$source").tar.gz" "$2"; exit 0; fi
  shift
done
''')
    curl.chmod(0o755)
    sha256sum = tmp_path / "sha256sum"
    sha256sum.write_text("#!/usr/bin/env bash\ncat >/dev/null\n")
    sha256sum.chmod(0o755)
    folder = tmp_path / "folder"
    folder.mkdir()
    script_path = tmp_path / "run.sh"
    script_path.write_text(render(ScriptParams("plan", "lambda", folder=str(folder))))
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "ARTIFACTS_DIR": str(tmp_path / "artifacts"), "ARTIFACT_PUT_URL_INIT_OUT": "https://artifact-upload", **{f"CACHE_GET_URL_{binary.upper()}": f"https://cache/{binary}" for binary in ("tofu", "tfsec", "infracost")}, **{f"CACHE_PUT_URL_{binary.upper()}": f"https://cache-put/{binary}" for binary in ("tofu", "tfsec", "infracost")}, **{f"UPSTREAM_URL_{binary.upper()}": f"https://upstream/{binary}" for binary in ("tofu", "tfsec", "infracost")}}
    completed = subprocess.run(["bash", str(script_path)], env=environment, text=True, capture_output=True, check=False)
    assert completed.returncode == 7
    assert "https://artifact-upload" in uploads.read_text()
    assert "init.out" in uploads.read_text()


def test_generated_script_extracts_per_binary_archives_and_executes_them(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    for binary in ("tofu", "tfsec", "infracost"):
        source = tmp_path / binary
        if binary == "tofu":
            body = '''case "$1" in
  init) echo init ;;
  validate) echo validate ;;
  plan) for arg in "$@"; do case "$arg" in -out=*) printf 'binary-plan' > "${arg#-out=}" ;; esac; done; echo plan ;;
esac'''
        elif binary == "tfsec":
            body = '''while [ "$#" -gt 0 ]; do
  if [ "$1" = "--out" ]; then out="$2"; break; fi
  shift
done
printf '%s' '{"results":[]}' > "$out"'''
        else:
            body = '''while [ "$#" -gt 0 ]; do
  if [ "$1" = "--out-file" ]; then out="$2"; break; fi
  shift
done
printf '%s' '{"totalMonthlyCost":"1.00"}' > "$out"
echo breakdown'''
        source.write_text(f"#!/usr/bin/env bash\n{body}\n")
        source.chmod(0o755)
        with tarfile.open(downloads / f"{binary}.tar.gz", "w:gz") as archive:
            archive.add(source, arcname=binary)
    curl = tmp_path / "curl"
    curl.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" --upload-file "* ]]; then exit 0; fi
for arg in "$@"; do
  case "$arg" in
    https://cache/*) exit 22 ;;
    https://upstream/*) source="$arg" ;;
  esac
done
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then cp "{downloads}/$(basename "$source").tar.gz" "$2"; exit 0; fi
  shift
done
''')
    curl.chmod(0o755)
    sha256sum = tmp_path / "sha256sum"
    sha256sum.write_text("#!/usr/bin/env bash\ncat >/dev/null\n")
    sha256sum.chmod(0o755)
    folder = tmp_path / "folder"
    folder.mkdir()
    script_path = tmp_path / "run.sh"
    script_path.write_text(render(ScriptParams("report", "lambda", folder=str(folder))))
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "ARTIFACTS_DIR": str(tmp_path / "artifacts"), **{f"ARTIFACT_PUT_URL_{name}": "https://upload" for name in ("INIT_OUT", "VALIDATE_OUT", "TF_PLAN_OUT", "TFSEC_JSON", "INFRACOST_JSON")}, "PLAN_BINARY_PUT_URL": "https://upload-plan", "PLAN_SHA256_PUT_URL": "https://upload-sha", "PLAN_METADATA_PUT_URL": "https://upload-metadata", "OPENCI_TF_PLAN_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan.tfplan", "OPENCI_TF_PLAN_SHA256_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan.tfplan.sha256", "OPENCI_TF_PLAN_METADATA_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan-metadata.json", "OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS": "2", "OPENCI_TF_REPO_NAME": "org/repo", "OPENCI_TF_PINNED_SHA": "a" * 40, "OPENCI_TF_ACCOUNT_ID": "123456789012", "OPENCI_TF_FOLDER": str(folder), "OPENCI_TF_ACTION": "report", "OPENCI_TF_TF_RUNTIME": "tofu:1.8.0", "OPENCI_TF_RUN_ID": "exec", "OPENCI_TF_RUN_ID": "0", "CACHE_GET_URL_TOFU": "https://cache/tofu", "CACHE_PUT_URL_TOFU": "https://cache/tofu", "UPSTREAM_URL_TOFU": "https://upstream/tofu", "CACHE_GET_URL_TFSEC": "https://cache/tfsec", "CACHE_PUT_URL_TFSEC": "https://cache/tfsec", "UPSTREAM_URL_TFSEC": "https://upstream/tfsec", "CACHE_GET_URL_INFRACOST": "https://cache/infracost", "CACHE_PUT_URL_INFRACOST": "https://cache/infracost", "UPSTREAM_URL_INFRACOST": "https://upstream/infracost"}
    completed = subprocess.run(["bash", str(script_path)], env=environment, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert "init" in completed.stdout
    assert (tmp_path / "artifacts" / "tfsec.json").exists()
    skip = json.loads((tmp_path / "artifacts" / "infracost.json").read_text())
    assert skip["skipped"] is True
