# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for evidence-backed report semantics (tfsec/infracost/error derivation)."""
from __future__ import annotations

import json
import os
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.engine.result import derive_error_from_steps
from src.domain.formatters.artifacts import (
    folder_comment,
    infracost,
    status_comment_in_progress,
    summary,
    tfsec,
)
from src.platform.aws.infracost_key import validate_infracost_key_path
from src.services.render import handler as render_handler
from src.services.run_folder import prepare_and_submit as prepare_handler
from tests.helpers.frozen_account import apply_prepare_handler_env, frozen_account_fields

_CLONE_TOKEN = "/openci-tf/clone-token/test"
_INFRACOST_KEY = "/openci-tf/infracost/api_key"
_PREPARE_BINDING = frozen_account_fields()
_FULL_SHA = "a" * 40

_TFSEC_WRITE = '''if echo "$*" | grep -q -- '--out'; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--out" ]; then out="$2"; break; fi
    shift
  done
  printf '%s' '{"results":[{"severity":"CRITICAL"},{"severity":"HIGH"}]}' > "$out"
else
  echo "CRITICAL finding"
fi
exit 0'''

_TFSEC_EMPTY = '''if echo "$*" | grep -q -- '--out'; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--out" ]; then out="$2"; break; fi
    shift
  done
  printf '%s' '{"results":[]}' > "$out"
else
  echo "No problems detected!"
fi
exit 0'''

_INFRACOST_WRITE = '''if [ "$1" = "output" ]; then
  echo "Monthly cost $1.00"
  exit 0
fi
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--out-file" ]; then out="$2"; break; fi
  shift
done
printf '%s' '{"totalMonthlyCost":"1.00"}' > "$out"
'''

def test_report_script_uses_tfsec_soft_fail_out_and_silent_curl():
    script = render(ScriptParams("report", "lambda", folder="infra"))
    assert "tfsec . --format json --soft-fail --out" in script
    assert "curl -sS --fail-with-body --retry 3" in script
    assert 'curl --fail-with-body --show-error --location "$upstream_url" -o "$archive"' in script
    assert 'curl --fail-with-body --show-error -H \'Content-Type: application/octet-stream\' --upload-file "$archive" "$cache_put_url"' in script
    put_lines = [line for line in script.splitlines() if '--upload-file "$archive"' in line and "cache_put_url" in line]
    assert put_lines
    assert all("--location" not in line for line in put_lines)
    assert "infracost breakdown" in script
    assert "tfsec . --soft-fail --no-color" in script
    assert "infracost output --path" in script
    assert 'printf \'%s\\n\' \'{"skipped":true,"reason":"not configured"}\'' in script


def test_report_script_upload_loop_includes_infracost_output():
    script = render(ScriptParams("report", "lambda", folder="infra"))
    upload_names = next(
        line.split("for name in ", 1)[1].split("; do", 1)[0]
        for line in script.splitlines()
        if line.strip().startswith("for name in ")
    )
    assert "infracost.output" in upload_names


def test_report_script_uploads_infracost_output_when_configured(tmp_path):
    uploads: list[str] = []
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
            body = _TFSEC_EMPTY
        else:
            body = _INFRACOST_WRITE
        source.write_text(f"#!/usr/bin/env bash\n{body}\n")
        source.chmod(0o755)
        with tarfile.open(downloads / f"{binary}.tar.gz", "w:gz") as archive:
            archive.add(source, arcname=binary)
    upload_log = tmp_path / "uploads.log"
    curl = tmp_path / "curl"
    curl.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" --upload-file "* ]]; then
  for arg in "$@"; do
    case "$arg" in
      --upload-file) file="$2" ;;
      https://*) url="$arg" ;;
    esac
  done
  printf '%s %s\\n' "$url" "$(basename "$file")" >> "{upload_log}"
  exit 0
fi
for arg in "$@"; do case "$arg" in https://cache/*) exit 22 ;; https://upstream/*) source="$arg" ;; esac; done
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
    artifacts = tmp_path / "artifacts"
    script_path = tmp_path / "run.sh"
    script_path.write_text(render(ScriptParams("report", "lambda", folder=str(folder))))
    infracost_output_url = "https://upload/infracost-output"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ARTIFACTS_DIR": str(artifacts),
        "INFRACOST_API_KEY": "ico-test",
        "ARTIFACT_PUT_URL_INIT_OUT": "https://upload/init",
        "ARTIFACT_PUT_URL_VALIDATE_OUT": "https://upload/validate",
        "ARTIFACT_PUT_URL_TF_PLAN_OUT": "https://upload/plan",
        "ARTIFACT_PUT_URL_TFSEC_JSON": "https://upload/tfsec-json",
        "ARTIFACT_PUT_URL_TFSEC_OUTPUT": "https://upload/tfsec-output",
        "ARTIFACT_PUT_URL_INFRACOST_JSON": "https://upload/infracost-json",
        "ARTIFACT_PUT_URL_INFRACOST_OUTPUT": infracost_output_url,
        "PLAN_BINARY_PUT_URL": "https://upload-plan",
        "PLAN_SHA256_PUT_URL": "https://upload-sha",
        "PLAN_METADATA_PUT_URL": "https://upload-metadata",
        "OPENCI_TF_PLAN_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan.tfplan",
        "OPENCI_TF_PLAN_SHA256_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan.tfplan.sha256",
        "OPENCI_TF_PLAN_METADATA_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan-metadata.json",
        "OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS": "2",
        "OPENCI_TF_REPO_NAME": "org/repo",
        "OPENCI_TF_PINNED_SHA": "a" * 40,
        "OPENCI_TF_ACCOUNT_ID": "123456789012",
        "OPENCI_TF_FOLDER": str(folder),
        "OPENCI_TF_ACTION": "report",
        "OPENCI_TF_TF_RUNTIME": "tofu:1.8.0",
        "OPENCI_TF_RUN_ID": "0",
        **{f"CACHE_GET_URL_{binary.upper()}": f"https://cache/{binary.lower()}" for binary in ("TOFU", "TFSEC", "INFRACOST")},
        **{f"CACHE_PUT_URL_{binary.upper()}": f"https://cache-put/{binary.lower()}" for binary in ("TOFU", "TFSEC", "INFRACOST")},
        **{f"UPSTREAM_URL_{binary.upper()}": f"https://upstream/{binary.lower()}" for binary in ("TOFU", "TFSEC", "INFRACOST")},
    }
    completed = subprocess.run(["bash", str(script_path)], env=env, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert (artifacts / "infracost.output").exists()
    logged = upload_log.read_text()
    assert infracost_output_url in logged


def test_plan_script_includes_scanners():
    script = render(ScriptParams("plan", "lambda"))
    assert "tfsec . --format json" in script
    assert "infracost breakdown" in script


def test_validate_script_is_not_publicly_renderable():
    with pytest.raises(ValueError, match="unsafe verb: validate"):
        render(ScriptParams("validate", "lambda"))


def test_derive_error_ignores_curl_progress_and_prefers_actionable_line():
    output = (
        '{"results":[{"severity":"CRITICAL"}]}\n'
        "  % Total    % Received % Xferd  Average Speed\n"
        "100  6068   0     0 100  6068     0 60720  --:--:-- --:--:-- --:--:-- 61292"
    )
    derived = derive_error_from_steps([{"status": "failed", "output": output, "exit_code": 1}])
    assert derived == "step failed with exit code 1"


def test_derive_error_prefers_error_line_over_curl_noise():
    output = (
        "100  6068   0     0 100  6068     0 60720  --:--:-- --:--:-- --:--:-- 61292\n"
        "Error: tfsec failed with exit code 127"
    )
    derived = derive_error_from_steps([{"status": "failed", "output": output}])
    assert derived == "Error: tfsec failed with exit code 127"


def test_derive_error_prefers_actionable_not_set_over_noise():
    output = "Dload  Upload   Total   Spent    Left  Speed\nAPI key not set"
    derived = derive_error_from_steps([{"status": "failed", "output": output}])
    assert derived == "API key not set"


def test_infracost_skip_marker_renders_not_configured():
    payload = '{"skipped":true,"reason":"not configured"}'
    rendered = infracost(payload)
    assert "not configured" in rendered
    assert "None" not in rendered


def test_status_comment_in_progress_matches_original_shape():
    execution_arn = "arn:aws:states:us-east-1:123456789012:execution:openci-tf:abc"
    console_url = f"https://console.aws.amazon.com/states/home?region=us-east-1#/executions/details/{execution_arn}"
    body = status_comment_in_progress(_FULL_SHA, console_url, "trigger-7-c42", now=1_700_000_000)
    assert body.startswith("\n## CI Details ")
    assert f"+ {_FULL_SHA}" in body
    assert f"+ [ci pipeline]({console_url})" in body
    assert "+ status: in_progress" in body
    assert body.endswith("#openci-tf:::status_comment\ttrigger-7-c42\t1700003600")


def test_summary_uses_icon_cells_without_legend():
    rendered = summary(
        [{"folder": "vpc", "succeeded": True, "account_id": "123456789012"}, {"folder": "eks", "succeeded": True, "account_id": "210987654321"}],
        {
            "vpc": {"tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy", "tfsec.json": '{"results":[]}', "infracost.json": '{"totalMonthlyCost":"0"}'},
            "eks": {"tf/plan.out": "Plan: 15 to add, 0 to change, 0 to destroy", "tfsec.json": '{"results":[{"severity":"LOW"}]}', "infracost.json": '{"totalMonthlyCost":"12.50"}'},
        },
        folder_urls={"vpc": "https://github.com/org/repo/pull/1#issuecomment-1", "eks": "https://github.com/org/repo/pull/1#issuecomment-2"},
        commit_hash="a" * 40,
        console_url="https://console.aws.amazon.com/states/home?region=us-east-1#/executions/details/arn",
    )
    assert "## Terraform Multi-Folder Summary" in rendered
    assert "[`vpc`](https://github.com/org/repo/pull/1#issuecomment-1)" in rendered
    assert "| [`vpc`](https://github.com/org/repo/pull/1#issuecomment-1) | `123456789012` | no changes | clean | $0 |" in rendered
    assert "| [`eks`](https://github.com/org/repo/pull/1#issuecomment-2) | `210987654321` | +15 ~0 -0 | low | $12.50 |" in rendered
    assert "Drift:" not in rendered
    assert "Security:" not in rendered
    assert "## CI Details" in rendered


def test_plan_all_summary_renders_drift_and_security_icons_for_each_folder():
    """Regression: tf plan all summary must show icon cells, not blank table columns."""
    rendered = summary(
        [
            {"folder": "terraform/ap-northeast-1", "succeeded": True, "account_id": "123456789012"},
            {"folder": "terraform/eu-west-1", "succeeded": True, "account_id": "210987654321"},
        ],
        {
            "terraform/ap-northeast-1": {
                "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
                "tfsec.json": '{"results":[]}',
                "infracost.json": '{"totalMonthlyCost":"0"}',
            },
            "terraform/eu-west-1": {
                "tf/plan.out": "Plan: 2 to add, 1 to change, 0 to destroy",
                "tfsec.json": '{"results":[{"severity":"HIGH"}]}',
                "infracost.json": '{"totalMonthlyCost":"5.00"}',
            },
        },
    )
    assert "| `terraform/ap-northeast-1` | `123456789012` | no changes | clean | $0 |" in rendered
    assert "| `terraform/eu-west-1` | `210987654321` | +2 ~1 -0 | high | $5.00 |" in rendered
    assert "Drift:" not in rendered


def test_summary_renders_not_configured_cost_column():
    rendered = summary(
        [{"folder": "infra/a", "succeeded": True, "account_id": "123456789012"}],
        {"infra/a": {"tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy", "tfsec.json": '{"results":[]}', "infracost.json": '{"skipped":true,"reason":"not configured"}'}},
    )
    assert "| `infra/a` | `123456789012` | no changes | clean | not configured |" in rendered


@pytest.mark.parametrize(
    ("tfsec_payload", "expected_security"),
    [
        pytest.param(None, "unknown", id="absent-folder-artifacts"),
        pytest.param("", "unknown", id="absent-tfsec"),
        pytest.param("{}", "unknown", id="empty-object"),
        pytest.param('{"skipped":true,"reason":"not run"}', "unknown", id="skipped-marker"),
        pytest.param("not-json tfsec output", "unknown", id="malformed-text"),
        pytest.param('{"results":["bad-entry"]}', "unknown", id="malformed-result-entry"),
        pytest.param('{"results":[{"severity":""}]}', "unknown", id="missing-severity"),
        pytest.param('{"results":[]}', "clean", id="valid-empty-results"),
        pytest.param('{"findings":[]}', "clean", id="valid-empty-findings"),
    ],
)
def test_summary_tfsec_security_unknown_unless_valid_empty(tfsec_payload, expected_security):
    artifacts: dict[str, str] = {"tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy"}
    if tfsec_payload is not None:
        artifacts["tfsec.json"] = tfsec_payload
    rendered = summary(
        [{"folder": "infra/a", "succeeded": True, "account_id": "123456789012"}],
        {"infra/a": artifacts},
    )
    assert f"| `infra/a` | `123456789012` | no changes | {expected_security} |" in rendered


def test_tfsec_formatter_omits_not_run_marker():
    assert tfsec('{"skipped":true,"reason":"not run"}') == ""


def test_tfsec_formatter_renders_json_findings_as_human_text():
    payload = json.dumps({
        "results": [
            {
                "severity": "HIGH",
                "rule_description": "S3 bucket should block public access",
                "location": {"filename": "main.tf", "start_line": 12, "end_line": 18},
                "resource": "aws_s3_bucket.logs",
            }
        ]
    })
    rendered = tfsec(payload)
    assert "Result #1 HIGH S3 bucket should block public access" in rendered
    assert "main.tf:12-18" in rendered
    assert "aws_s3_bucket.logs" in rendered
    assert '{"results"' not in rendered


def test_tfsec_formatter_handles_empty_and_malformed_json():
    assert "No problems detected" in tfsec('{"results":[]}')
    rendered = tfsec("not-json but readable output")
    assert "not-json but readable output" in rendered


def test_bound_comment_truncates_large_bodies():
    from src.domain.formatters.artifacts import bound_comment

    body = "x" * 70_000
    rendered = bound_comment(body, max_chars=1000)
    assert len(rendered) <= 1000
    assert "truncated" in rendered.lower()


def test_bound_comment_preserves_suffix_tag_exactly_once():
    from src.domain.formatters.artifacts import _MAX_COMMENT_CHARS, bound_comment

    tag = "#openci-tf:::tag::deadbeef"
    body = "y" * (_MAX_COMMENT_CHARS + 10_000)
    rendered = bound_comment(body, suffix=f"\n\n{tag}")
    assert len(rendered) <= _MAX_COMMENT_CHARS
    assert rendered.endswith(f"\n\n{tag}")
    assert rendered.count(tag) == 1
    assert "truncated" in rendered.lower()


def test_bound_comment_leaves_normal_bodies_unchanged():
    from src.domain.formatters.artifacts import bound_comment

    body = "## Terraform\n\nAll good."
    tag = "#openci-tf:::tag::abc123"
    suffix = f"\n\n{tag}"
    assert bound_comment(body, suffix=suffix) == body + suffix


def test_render_delete_and_repost_retains_marker_when_body_exceeds_github_limit(monkeypatch):
    from src.domain.formatters.artifacts import _MAX_COMMENT_CHARS
    from src.domain.github.comment_object_id import format_comment_object_marker

    class Client:
        def __init__(self):
            self.bodies: list[str] = []
            self.ids = 5202721251

        def find_comments_by_tag(self, repo, pr, tag):
            return []

        def create_comment(self, repo, pr, body):
            self.bodies.append(body)
            return self.ids

    client = Client()
    monkeypatch.setattr(render_handler, "get_github_token", lambda _: "token")
    marker = format_comment_object_marker("<REPO_ORG>/<REPO_NAME>", 1, "plan", "infra/a")
    huge = "z" * (_MAX_COMMENT_CHARS + 5_000)
    comment_id = render_handler._delete_and_repost(
        client, "<REPO_ORG>/<REPO_NAME>", 1, huge, "plan", "infra/a"
    )
    assert comment_id == 5202721251
    body = client.bodies[0]
    assert len(body) <= _MAX_COMMENT_CHARS
    assert body.count(marker) == 1
    assert body.endswith(marker)
    assert "truncated" in body.lower()


def test_render_repeated_delete_and_repost_replaces_comment_with_new_id():
    from src.domain.github.comment_object_id import format_comment_object_marker
    from src.platform.github.client import GitHubClient

    class Session:
        def __init__(self):
            self.store: dict[int, str] = {}
            self.next_id = 5202721251
            self.deleted: list[int] = []

        def post(self, url, json=None):
            if json is None:
                raise AssertionError("unexpected GET disguised as POST")
            cid = self.next_id
            self.next_id += 1
            self.store[cid] = json["body"]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": cid})

        def patch(self, url, json=None):
            raise AssertionError("generated comments must not be PATCHed")

        def delete(self, url):
            cid = int(url.rsplit("/", 1)[-1])
            self.deleted.append(cid)
            del self.store[cid]
            return SimpleNamespace(raise_for_status=lambda: None)

        def get(self, url, params=None):
            page = (params or {}).get("page", 1)
            if page > 1:
                comments = []
            else:
                comments = [
                    {"id": cid, "body": body, "user": {"login": "openci-bot"}}
                    for cid, body in self.store.items()
                ]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: comments)

    session = Session()
    client = GitHubClient("token")
    client.session = session
    client._token_login = "openci-bot"
    repo, pr = "<REPO_ORG>/<REPO_NAME>", 1
    marker = format_comment_object_marker(repo, pr, "plan", "infra/a")
    huge = "w" * 70_000
    first_id = render_handler._delete_and_repost(client, repo, pr, huge, "plan", "infra/a")
    second_id = render_handler._delete_and_repost(
        client, repo, pr, huge + " updated", "plan", "infra/a"
    )
    assert first_id == 5202721251
    assert second_id == 5202721252
    assert session.deleted == [first_id]
    assert len(session.store) == 1
    assert session.store[second_id].count(marker) == 1


def test_validate_infracost_key_path_rejects_broad_or_foreign_paths():
    assert validate_infracost_key_path("/openci-tf/infracost/api_key") == "/openci-tf/infracost/api_key"
    with pytest.raises(ValueError, match="must be under"):
        validate_infracost_key_path("/openci-tf/clone-token/test")
    with pytest.raises(ValueError, match="must be under"):
        validate_infracost_key_path("/ssm/other/key")


def _prepare_env(monkeypatch, tmp_path):
    apply_prepare_handler_env(monkeypatch)
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setattr(prepare_handler.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    apply_prepare_handler_env(monkeypatch)
    monkeypatch.setattr(prepare_handler.sts, "assume_role", lambda *_args, **_kwargs: {"AWS_ACCESS_KEY_ID": "target"})
    monkeypatch.setattr(prepare_handler.s3, "presign_get", lambda *_: "get-url")
    monkeypatch.setattr(prepare_handler.s3, "presign_put", lambda *_args, **_kwargs: "put-url")
    monkeypatch.setattr(prepare_handler.s3, "presign_create_put", lambda *_: "create-put-url")
    monkeypatch.setattr(prepare_handler, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path))
    monkeypatch.setattr(prepare_handler, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_handler, "get_github_token", lambda _: "github-token")
    monkeypatch.setattr(prepare_handler.s3, "upload_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_handler.s3, "head_object", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_handler.engine, "invoke_init_job", lambda *_, **__: None)
    folder = tmp_path / "folder"
    folder.mkdir(exist_ok=True)
    return folder


def test_prepare_plan_injects_infracost_key_only_into_encrypted_secrets(monkeypatch, tmp_path):
    _prepare_env(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def encrypt(plain, _kms):
        captured["secrets"] = json.loads(Path(plain).read_text())
        return plain

    monkeypatch.setattr(prepare_handler.sops, "encrypt_file", encrypt)
    monkeypatch.setattr(prepare_handler, "build_package", lambda *_: str(tmp_path / "package.zip"))
    monkeypatch.setattr("src.services.run_folder.secrets.get_infracost_api_key", lambda _: "ico-test-key")
    prepare_handler.handler({
        "action": "plan", "run_id": "run", "folder": "infra/app", "budget": 900, "deadline_at": "2999-01-01T00:00:00Z", "attempt": 0,
        "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"},
        "folder_config": {"account_alias": "target"}, "git_url": "https://github.com/org/repo.git",
        "commit_hash": _FULL_SHA, "ssm_openci_tf_github_token": _CLONE_TOKEN, "repo_name": "org/repo",
        "ssm_infracost_api_key": _INFRACOST_KEY,
        **_PREPARE_BINDING,
    }, object())
    assert captured["secrets"]["INFRACOST_API_KEY"] == "ico-test-key"


def test_prepare_report_injects_infracost_key_only_into_encrypted_secrets(monkeypatch, tmp_path):
    _prepare_env(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def encrypt(plain, _kms):
        captured["secrets"] = json.loads(Path(plain).read_text())
        return plain

    monkeypatch.setattr(prepare_handler.sops, "encrypt_file", encrypt)
    monkeypatch.setattr(prepare_handler, "build_package", lambda *_: str(tmp_path / "package.zip"))
    monkeypatch.setattr("src.services.run_folder.secrets.get_infracost_api_key", lambda _: "ico-test-key")
    prepare_handler.handler({
        "action": "report", "run_id": "run", "folder": "infra/app", "budget": 900, "deadline_at": "2999-01-01T00:00:00Z", "attempt": 0,
        "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"},
        "folder_config": {"account_alias": "target"}, "git_url": "https://github.com/org/repo.git",
        "commit_hash": _FULL_SHA, "ssm_openci_tf_github_token": _CLONE_TOKEN, "repo_name": "org/repo",
        "ssm_infracost_api_key": _INFRACOST_KEY,
        **_PREPARE_BINDING,
    }, object())
    secrets = captured["secrets"]
    assert secrets["INFRACOST_API_KEY"] == "ico-test-key"
    assert list(secrets.values()).count("ico-test-key") == 1


def test_prepare_report_without_infracost_setting_omits_api_key(monkeypatch, tmp_path):
    _prepare_env(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    fetch = Mock()

    def encrypt(plain, _kms):
        captured["secrets"] = json.loads(Path(plain).read_text())
        return plain

    monkeypatch.setattr(prepare_handler.sops, "encrypt_file", encrypt)
    monkeypatch.setattr(prepare_handler, "build_package", lambda *_: str(tmp_path / "package.zip"))
    monkeypatch.setattr("src.services.run_folder.secrets.get_infracost_api_key", fetch)
    prepare_handler.handler({
        "action": "report", "run_id": "run", "folder": "infra/app", "budget": 900, "deadline_at": "2999-01-01T00:00:00Z", "attempt": 0,
        "upstream_urls": {"tofu": "https://tofu", "tfsec": "https://tfsec", "infracost": "https://infracost"},
        "folder_config": {"account_alias": "target"}, "git_url": "https://github.com/org/repo.git",
        "commit_hash": _FULL_SHA, "ssm_openci_tf_github_token": _CLONE_TOKEN, "repo_name": "org/repo",
        "ssm_infracost_api_key": "",
        **_PREPARE_BINDING,
    }, object())
    fetch.assert_not_called()
    assert "INFRACOST_API_KEY" not in captured["secrets"]


def _run_report_script(tmp_path: Path, *, tfsec_body: str | None = None, infracost: bool = False) -> subprocess.CompletedProcess[str]:
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
            body = tfsec_body or _TFSEC_EMPTY
        else:
            body = _INFRACOST_WRITE
        source.write_text(f"#!/usr/bin/env bash\n{body}\n")
        source.chmod(0o755)
        with tarfile.open(downloads / f"{binary}.tar.gz", "w:gz") as archive:
            archive.add(source, arcname=binary)
    curl = tmp_path / "curl"
    curl.write_text(f'''#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" --upload-file "* ]]; then exit 0; fi
for arg in "$@"; do case "$arg" in https://cache/*) exit 22 ;; https://upstream/*) source="$arg" ;; esac; done
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
    artifacts = tmp_path / "artifacts"
    script_path = tmp_path / "run.sh"
    script_path.write_text(render(ScriptParams("report", "lambda", folder=str(folder))))
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ARTIFACTS_DIR": str(artifacts),
        **{f"ARTIFACT_PUT_URL_{name}": "https://upload" for name in ("INIT_OUT", "VALIDATE_OUT", "TF_PLAN_OUT", "TFSEC_JSON", "TFSEC_OUTPUT", "INFRACOST_JSON")},
        "PLAN_BINARY_PUT_URL": "https://upload-plan",
        "PLAN_SHA256_PUT_URL": "https://upload-sha",
        "PLAN_METADATA_PUT_URL": "https://upload-metadata",
        "OPENCI_TF_PLAN_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan.tfplan",
        "OPENCI_TF_PLAN_SHA256_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan.tfplan.sha256",
        "OPENCI_TF_PLAN_METADATA_S3_URI": "s3://tmp/plans/repo/sha/account/folder/execution/attempt/plan-metadata.json",
        "OPENCI_TF_PLAN_EXPIRES_AFTER_DAYS": "2",
        "OPENCI_TF_REPO_NAME": "org/repo",
        "OPENCI_TF_PINNED_SHA": "a" * 40,
        "OPENCI_TF_ACCOUNT_ID": "123456789012",
        "OPENCI_TF_FOLDER": str(folder),
        "OPENCI_TF_ACTION": "report",
        "OPENCI_TF_TF_RUNTIME": "tofu:1.8.0",
        "OPENCI_TF_RUN_ID": "0",
        **{f"CACHE_GET_URL_{binary.upper()}": f"https://cache/{binary.lower()}" for binary in ("TOFU", "TFSEC", "INFRACOST")},
        **{f"CACHE_PUT_URL_{binary.upper()}": f"https://cache-put/{binary.lower()}" for binary in ("TOFU", "TFSEC", "INFRACOST")},
        **{f"UPSTREAM_URL_{binary.upper()}": f"https://upstream/{binary.lower()}" for binary in ("TOFU", "TFSEC", "INFRACOST")},
    }
    if infracost:
        env["INFRACOST_API_KEY"] = "ico-test"
    return subprocess.run(["bash", str(script_path)], env=env, text=True, capture_output=True, check=False)


def test_report_script_succeeds_with_tfsec_findings_and_writes_clean_json(tmp_path):
    completed = _run_report_script(tmp_path, tfsec_body=_TFSEC_WRITE)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "artifacts" / "tfsec.json").read_text())
    assert payload["results"]
    assert (tmp_path / "artifacts" / "tfsec.output").exists()
    skip = json.loads((tmp_path / "artifacts" / "infracost.json").read_text())
    assert skip["skipped"] is True
    assert not (tmp_path / "artifacts" / "infracost.output").exists()


def test_report_script_fails_loudly_on_tfsec_operational_failure(tmp_path):
    tfsec_body = 'exit 127'
    completed = _run_report_script(tmp_path, tfsec_body=tfsec_body)
    assert completed.returncode == 127
    assert "Error: tfsec failed with exit code 127" in completed.stderr


def test_report_script_fails_loudly_on_invalid_tfsec_json(tmp_path):
    tfsec_body = '''while [ "$#" -gt 0 ]; do
  if [ "$1" = "--out" ]; then out="$2"; break; fi
  shift
done
printf '%s' 'not-json' > "$out"
exit 0'''
    completed = _run_report_script(tmp_path, tfsec_body=tfsec_body)
    assert completed.returncode == 11
    assert "Error: tfsec produced invalid JSON" in completed.stderr


def test_report_script_runs_infracost_when_configured(tmp_path):
    completed = _run_report_script(tmp_path, tfsec_body=_TFSEC_EMPTY, infracost=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / "artifacts" / "infracost.json").read_text())["totalMonthlyCost"] == "1.00"
    assert (tmp_path / "artifacts" / "infracost.output").exists()


def test_folder_comment_parses_both_report_artifacts(tmp_path):
    artifacts = {
        "init.out": "initialized",
        "validate.out": "Success!",
        "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
        "tfsec.json": json.dumps({"results": [{"severity": "HIGH"}]}),
        "infracost.json": json.dumps({"skipped": True, "reason": "not configured"}),
    }
    rendered = folder_comment(
        "infra/a",
        {"status": "succeeded", "account_id": "123456789012"},
        artifacts,
        action="report",
        existing_names=frozenset(artifacts),
        tmp_bucket="tmp-bucket",
        region="us-east-1",
        hub_account_id="999999999999",
        identity_center_start_url="https://d-9567aa6b98.awsapps.com/start",
        identity_center_role_name="AWSAdministratorAccess",
    )
    assert "infra/a · Drift" in rendered
    assert "> <summary>Security" in rendered
    assert "not configured" in rendered
    assert "> <summary>Setup" in rendered


def test_render_plan_all_posts_linked_multi_folder_summary(monkeypatch):
    """Production-shaped: tf plan all final render posts summary with linked rows and icon cells."""
    from types import SimpleNamespace

    from src.platform.github.client import comment_url

    webhook = {
        "repo_name": "org/repo",
        "pr_number": 7,
        "commit_hash": _FULL_SHA,
        "trigger_id": "trigger",
        "event_type": "issue_comment",
        "comment_id": 42,
    }
    artifacts = {
        "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
        "tfsec.json": '{"results":[]}',
        "infracost.json": '{"totalMonthlyCost":"0"}',
    }
    posted_bodies: dict[str, str] = {}
    comment_ids = {"infra/a": 101, "infra/b": 102}

    def upsert(_client, repo, pr, body, action, folder, **kwargs):
        if folder == "infra/a":
            cid = comment_ids["infra/a"]
            slot = "folder-infra/a"
        elif folder == "infra/b":
            cid = comment_ids["infra/b"]
            slot = "folder-infra/b"
        else:
            cid = 999
            slot = "summary"
        posted_bodies[slot] = body
        return cid

    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render_handler.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render_handler, "list_text_prefix", lambda *_args, **_kw: artifacts)
    monkeypatch.setattr(
        render_handler,
        "list_prefix_object_names",
        lambda *_args, **_kw: frozenset(artifacts),
    )
    monkeypatch.setattr(render_handler, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render_handler.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render_handler, "GitHubClient", lambda _: SimpleNamespace(delete_comment=lambda *_a, **_k: None))
    monkeypatch.setattr(render_handler, "_delete_and_repost", upsert)
    monkeypatch.setattr(render_handler, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render_handler, "_delete_transient_status_comment", lambda *_, **__: [])

    render_handler.handler(
        {
            "action": "plan",
            "webhook_info": webhook,
            "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
            "outcomes": [
                {"folder": "infra/a", "account_id": "123456789012", "execution_id": "run.a.0", "succeeded": True},
                {"folder": "infra/b", "account_id": "210987654321", "execution_id": "run.b.0", "succeeded": True},
            ],
            "skipped": [],
        },
        None,
    )

    summary_body = posted_bodies["summary"]
    assert "## Terraform Multi-Folder Summary" in summary_body
    assert f"[`infra/a`]({comment_url('org/repo', 7, comment_ids['infra/a'])})" in summary_body
    assert f"[`infra/b`]({comment_url('org/repo', 7, comment_ids['infra/b'])})" in summary_body
    assert "| no changes | clean | $0 |" in summary_body
    assert "Legend" not in summary_body
    assert "Drift:" not in summary_body
