"""Production seam tests for PR-scoped artifact layout (prepare/collect/render path)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.domain.engine.artifact_paths import (
    build_folder_artifact_keys,
    build_folder_artifact_keys_for_run,
    expected_plan_artifact_uris,
    manifest_key,
    pr_pointer_key,
)
from src.domain.engine.run_artifact_layout import resolve_run_artifact_layout
from src.platform.aws.run_registry import RunRegistryError, get_run
from tests.unit.manifest_fixtures import complete_plan_object_mocks


_REPO_ROOT = Path(__file__).resolve().parents[2]
_LAMBDAS_TF = (_REPO_ROOT / "infra/deploy/modules/run_folder/lambdas.tf").read_text()
_IAM_TF = (_REPO_ROOT / "infra/deploy/modules/run_folder/iam.tf").read_text()


def _github_pr_run_record(pr_number: int) -> dict:
    return {
        "repo_name": "org/repo",
        "notification_target": {"type": "github_pr", "pr_number": pr_number},
    }


def _prepare_handler_mocks(monkeypatch, tmp_path):
    from src.services.run_folder import prepare_and_submit as prepare_handler

    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setenv("LANE_MODE", "read")
    monkeypatch.setattr(
        prepare_handler.boto3,
        "Session",
        lambda: SimpleNamespace(get_credentials=lambda: None),
    )
    monkeypatch.setattr(
        prepare_handler.sts, "get_caller_account_id", lambda: "REPLACE_MAIN_ACCOUNT"
    )
    monkeypatch.setattr(
        prepare_handler,
        "load_account_alias",
        lambda _: SimpleNamespace(
            account_id="123456789012",
            role_name="target",
            external_id="openci-tf-6be00970ed31c57d",
            max_ttl=3600,
        ),
    )
    monkeypatch.setattr(
        prepare_handler.sts,
        "assume_role",
        lambda *_, **__: {"AWS_ACCESS_KEY_ID": "target"},
    )
    monkeypatch.setattr(prepare_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        prepare_handler, "shallow_clone", lambda *_args, **_kwargs: str(tmp_path)
    )
    monkeypatch.setattr(prepare_handler, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_handler.sops, "encrypt_file", lambda path, _: path)
    monkeypatch.setattr(
        prepare_handler,
        "build_package",
        lambda *_args, **_kwargs: str(tmp_path / "package.zip"),
    )
    monkeypatch.setattr(
        prepare_handler.s3, "upload_file", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        prepare_handler.s3, "head_object", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(prepare_handler.engine, "invoke_init_job", lambda *_: None)
    return prepare_handler


def test_prepare_presigns_only_pr_scoped_artifact_keys(monkeypatch, tmp_path):
  """Registry-backed PR plan prepare must presign scoped keys only."""
  prepare_handler = _prepare_handler_mocks(monkeypatch, tmp_path)
  presigned_keys: list[str] = []

  def capture_presign_put(_bucket: str, key: str, _expiry: int) -> str:
      presigned_keys.append(key)
      return f"put://{key}"

  monkeypatch.setattr(prepare_handler.s3, "presign_put", capture_presign_put)
  monkeypatch.setattr(prepare_handler.s3, "presign_get", lambda *_: "get-url")
  monkeypatch.setattr(
      prepare_handler.s3,
      "presign_create_put",
      lambda *_args, **_kwargs: "create-put-url",
  )
  monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
  monkeypatch.setattr(
      "src.domain.engine.run_artifact_layout.get_run",
      lambda run_id: _github_pr_run_record(17),
  )

  repo_name = "org/repo"
  run_id = "1786799413753.ce05146a"
  folder = "terraform/example"
  keys = build_folder_artifact_keys_for_run(
      repo_name=repo_name,
      run_id=run_id,
      folder_path=folder,
      pr_number=17,
      pointer_type="plan",
  )
  result = prepare_handler.handler(
      {
          "action": "plan",
          "run_id": run_id,
          "folder": folder,
          "budget": 900, "deadline_at": "2999-01-01T00:00:00Z",
          "attempt": 0,
          "upstream_urls": {
              "tofu": "https://tofu",
              "tfsec": "https://tfsec",
              "infracost": "https://infracost",
          },
          "folder_config": {"account_alias": "target"},
          "git_url": "https://github.com/org/repo.git",
          "commit_hash": "a" * 40,
          "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
          "repo_name": repo_name,
      },
      object(),
  )
  assert result["plan_metadata_uri"] == f"s3://tmp/{keys.plan_metadata}"
  assert "/pr-17/" in result["plan_metadata_uri"]
  artifact_keys = [key for key in presigned_keys if key.startswith("openci-tf/")]
  assert artifact_keys
  assert all("/pr-17/" in key for key in artifact_keys)
  assert not any(f"/{run_id}/" in key and "/pr-17/" not in key for key in artifact_keys)
  assert keys.plan_tfplan in artifact_keys
  assert keys.plan_sha256 in artifact_keys
  assert keys.plan_metadata in artifact_keys
  assert keys.init_out in artifact_keys


def test_collect_on_scoped_objects_creates_scoped_manifest_and_plan_env(monkeypatch):
  """Collect uses production layout resolver and publishes scoped plan.env."""
  from src.services.run_folder import collect

  repo_name = "org/repo"
  run_id = "1700000000100.deadbeef"
  folder = "infra/a"
  exec_id = "run.abc.0"
  pr_number = 17
  keys = build_folder_artifact_keys_for_run(
      repo_name=repo_name,
      run_id=run_id,
      folder_path=folder,
      pr_number=pr_number,
      pointer_type="plan",
  )
  published: list[tuple[str, str]] = []
  committed_manifest: dict = {}

  monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
  monkeypatch.setenv("DONE_BUCKET_NAME", "done")
  monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
  monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
  monkeypatch.setattr(
      "src.domain.engine.run_artifact_layout.get_run",
      lambda _run_id: _github_pr_run_record(pr_number),
  )
  monkeypatch.setattr(
      collect,
      "put_json_create_only",
      lambda _bucket, _key, manifest: committed_manifest.update(manifest) or "v1",
  )
  monkeypatch.setattr(
      collect,
      "publish_execution_pointer",
      lambda **kwargs: published.append((kwargs["key"], kwargs["execution_id"])),
  )
  monkeypatch.setattr(collect, "put_folder_attempt", lambda **_kwargs: None)
  _lm = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
  plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
      execution_id=exec_id,
      repo_name=repo_name,
      run_id=run_id,
      commit_hash="a" * 40,
      account_id="123456789012",
      folder=folder,
      attempt=0,
      last_modified=_lm,
      pr_number=pr_number,
      pointer_type="plan",
  )

  def head_without_drift(bucket: str, key: str):
      if key == keys.drift_json:
          return None
      return head_object(bucket, key)

  monkeypatch.setattr(collect, "head_object", head_without_drift)
  monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
  monkeypatch.setattr(
      collect, "get_bounded_json", lambda *_args, **_kwargs: plan_metadata
  )
  expected = expected_plan_artifact_uris(
      bucket="tmp",
      repo_name=repo_name,
      run_id=run_id,
      folder_path=folder,
      pr_number=pr_number,
      pointer_type="plan",
  )
  collected = collect.handler(
      {
          "exec_id": exec_id,
          "succeeded": True,
          "steps": [],
          "pointers": {
              "done": f"s3://done/{exec_id}/done",
              "plan_metadata": expected.metadata,
          },
          "action": "plan",
          "repo_name": repo_name,
          "commit_hash": "a" * 40,
          "account_id": "123456789012",
          "folder": folder,
          "run_id": run_id,
          "attempt": 0,
          "plan_metadata_uri": expected.metadata,
      },
      object(),
  )
  scoped_manifest_key = manifest_key(
      repo_name, run_id, folder, pr_number=pr_number, pointer_type="plan"
  )
  assert collected["succeeded"] is True
  assert collected["manifest_s3_uri"] == f"s3://tmp/{scoped_manifest_key}"
  assert "/pr-17/" in collected["manifest_s3_uri"]
  assert published[0][0] == pr_pointer_key(
      repo_name=repo_name,
      pr_number=pr_number,
      folder_path=folder,
      pointer_type="plan",
  )
  assert committed_manifest["manifest_s3_uri"] == collected["manifest_s3_uri"]
  assert committed_manifest["pr_number"] == pr_number
  assert committed_manifest["pointer_type"] == "plan"


def test_resolve_run_artifact_layout_fails_loud_on_registry_client_error(monkeypatch):
  monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
  error = ClientError(
      {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
      "GetItem",
  )
  monkeypatch.setattr(
      "src.domain.engine.run_artifact_layout.get_run",
      lambda _run_id: (_ for _ in ()).throw(error),
  )
  with pytest.raises(RunRegistryError, match="run registry lookup failed"):
      resolve_run_artifact_layout(
          repo_name="org/repo",
          run_id="run-1",
          folder_path="infra/a",
          action="plan",
      )


@patch("src.platform.aws.run_registry._shared._table")
def test_get_run_requests_consistent_read(mock_table):
  table = MagicMock()
  table.get_item.return_value = {"Item": None}
  mock_table.return_value = table

  get_run("run-consistent")

  table.get_item.assert_called_once()
  assert table.get_item.call_args.kwargs.get("ConsistentRead") is True


def test_legacy_layout_without_registry_or_without_pr_target(monkeypatch):
  monkeypatch.delenv("RUN_REGISTRY_TABLE_NAME", raising=False)
  legacy = resolve_run_artifact_layout(
      repo_name="org/repo",
      run_id="legacy-run",
      folder_path="infra/a",
      action="plan",
  )
  keys = build_folder_artifact_keys(
      repo_name="org/repo", run_id="legacy-run", folder_path="infra/a"
  )
  assert legacy.folder_keys.prefix == keys.prefix
  assert legacy.pr_number is None
  assert "/pr-" not in legacy.folder_keys.prefix

  monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
  monkeypatch.setattr(
      "src.domain.engine.run_artifact_layout.get_run",
      lambda _run_id: {"repo_name": "org/repo", "notification_target": {"type": "api"}},
  )
  non_pr = resolve_run_artifact_layout(
      repo_name="org/repo",
      run_id="api-run",
      folder_path="infra/a",
      action="plan",
  )
  assert non_pr.pr_number is None
  assert "/pr-" not in non_pr.folder_keys.prefix


def test_read_lane_lambdas_receive_run_registry_table_name():
  assert "prepare-and-submit" in _LAMBDAS_TF
  assert (
      'each.key == "persist-retry-attempt" || each.key == "write-failure-manifest" || each.key == "collect" || each.key == "prepare-and-submit"'
      in _LAMBDAS_TF
  )
  assert "RUN_REGISTRY_TABLE_NAME" in _LAMBDAS_TF


def test_collect_and_failure_manifest_iam_include_registry_get_item():
  collect_start = _IAM_TF.index('aws_iam_role_policy" "collect"')
  collect_end = _IAM_TF.index('resource "aws_iam_role_policy"', collect_start + 10)
  collect_block = _IAM_TF[collect_start:collect_end]
  assert "dynamodb:GetItem" in collect_block
  assert "dynamodb:PutItem" in collect_block
  assert "dynamodb:UpdateItem" in collect_block
  assert "dynamodb:TransactWriteItems" in collect_block
  assert "${var.project_name}-run-registry" in collect_block

  failure_start = _IAM_TF.index('aws_iam_role_policy" "write_failure_manifest"')
  failure_end = _IAM_TF.index('resource "aws_iam_role_policy"', failure_start + 10)
  failure_block = _IAM_TF[failure_start:failure_end]
  assert "dynamodb:GetItem" in failure_block
  assert "${var.project_name}-run-registry" in failure_block


def test_write_failure_manifest_uses_scoped_layout_for_pr_runs(monkeypatch):
  from src.services.run_folder import write_failure_manifest

  repo_name = "org/repo"
  run_id = "1700000000400.deadbeef"
  folder = "infra/a"
  pr_number = 17
  scoped_manifest_key = manifest_key(
      repo_name, run_id, folder, pr_number=pr_number, pointer_type="plan"
  )
  persisted_key: str | None = None
  persisted_manifest: dict = {}

  def capture_put(_bucket: str, key: str, _manifest: dict) -> str:
      nonlocal persisted_key
      persisted_key = key
      persisted_manifest.update(_manifest)
      return "v1"

  monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
  monkeypatch.setenv("DONE_BUCKET_NAME", "done")
  monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
  monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
  monkeypatch.setattr(
      "src.domain.engine.run_artifact_layout.get_run",
      lambda _run_id: _github_pr_run_record(pr_number),
  )
  monkeypatch.setattr(
      write_failure_manifest,
      "put_json_create_only",
      capture_put,
  )
  monkeypatch.setattr(
      write_failure_manifest,
      "put_folder_attempt",
      lambda **_kwargs: None,
  )
  summary = write_failure_manifest.handler(
      {
          "run_id": run_id,
          "folder": folder,
          "action": "plan",
          "account_id": "123456789012",
          "attempt": 0,
          "failure_reason": "prepare failed",
          "repo_name": repo_name,
          "commit_hash": "a" * 40,
          "submitted_at": 1_700_000_000.0,
      },
      object(),
  )
  assert summary["succeeded"] is False
  assert summary["manifest_s3_uri"] == f"s3://tmp/{scoped_manifest_key}"
  assert "/pr-17/" in summary["manifest_s3_uri"]
  assert persisted_key == scoped_manifest_key
  assert persisted_manifest["pr_number"] == pr_number
  assert persisted_manifest["pointer_type"] == "plan"
