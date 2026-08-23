"""Behavioral probes over rendered ASL/data transformations from remediation round 2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from src.core.models import FolderConfig, MutationVerbConfig, RepoSettings
from src.domain.engine.inner_run_folder_state import (
    collect_task_parameters,
    route_after_placeholder_failure,
)
from src.domain.engine.artifact_paths import manifest_key, pr_pointer_key
from src.domain.engine.manifest import build_failure_manifest, validate_manifest_schema
from src.domain.engine.outer_map_state import merge_map_item
from src.domain.intent.gates import evaluate_intent_gates
from src.domain.intent.plan_lookup import (
    _folder_plan_sha256,
    find_newest_fresh_plan_run,
)
from src.services.run_folder import (
    collect,
    persist_retry_attempt,
    prepare_and_submit,
    write_failure_manifest,
)
from tests.unit.manifest_fixtures import (
    committed_success_plan_manifest,
    complete_plan_object_mocks,
    plan_metadata_dict,
)


def _terraform_block(path: str, state_name: str) -> str:
    source = Path(path).read_text(encoding="utf-8")
    return source.split(f"{state_name} = {{", 1)[1].split("\n      }", 1)[0]


def _plan_map_item() -> dict:
    return {
        "run_id": "r" * 32,
        "folder": "infra/vpc",
        "account_id": "123456789012",
        "b": [
            "openci-tf-executor-readonly",
            None,
            "openci-tf-0123456789abcdef",
            3600,
        ],
        "action": "plan",
        "attempt": 0,
        "budget": 3600, "deadline_at": "2999-01-01T00:00:00Z",
        "c": {"account_alias": "target", "tf_runtime": "tofu:1.8.0"},
        "e": f"{'r' * 32}.infra/vpc.0",
    }


def _shared_map_context() -> dict:
    return {
        "upstream_urls": {"tofu:1.8.0": "https://example.com/tofu"},
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
    }


def test_normal_plan_map_item_without_pin_selects_safe_outer_fields():
    block = _terraform_block(
        "infra/deploy/modules/openci_tf/step_function.tf", "RunFolders"
    )
    assert "folder_pin" not in block
    assert "source_plan_run_id" not in block
    merged = merge_map_item(_shared_map_context(), _plan_map_item())
    assert "folder_pin" not in merged
    assert "source_plan_run_id" not in merged


def test_mutation_sequential_map_item_selector_keeps_pin_fields():
    block = _terraform_block(
        "infra/deploy/modules/openci_tf/step_function_mutation_outer.tf",
        "RunFoldersSequential",
    )
    assert '"folder_pin.$"' in block
    assert '"source_plan_run_id.$"' in block
    assert '"grace_seconds.$"' in block


def test_placeholder_render_failure_routes_to_read_step_gate_or_mutation_map():
    block = _terraform_block(
        "infra/deploy/modules/openci_tf/step_function.tf", "RenderPlaceholder"
    )
    assert 'Next = "NextStep"' in block
    assert "RouteFolderConcurrency" not in block
    assert route_after_placeholder_failure("apply") == "RunFoldersSequential"
    assert route_after_placeholder_failure("destroy") == "RunFoldersSequential"
    assert route_after_placeholder_failure("plan") == "NextStep"


def test_both_credential_retry_branches_preserve_mutation_pins():
    base = {
        "run_id": "r" * 32,
        "folder": "infra/vpc",
        "action": "apply",
        "budget": 3600, "deadline_at": "2999-01-01T00:00:00Z",
        "folder_config": {"account_alias": "target", "tf_runtime": "tofu:1.8.0"},
        "upstream_urls": {"tofu:1.8.0": "https://example.com/tofu"},
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "account_id": "123456789012",
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
        "folder_pin": {
            "source_run_id": "plan-run",
            "plan_sha256": "b" * 64,
            "plan_artifact_name": "plan.tfplan",
            "account_id": "123456789012",
            "tf_runtime": "tofu:1.8.0",
        },
        "source_plan_run_id": "plan-run",
        "result": {"attempt": 0, "exec_id": "run.infra.0", "submitted_at": 1.0},
        "attempt": 0,
    }
    from_probe = persist_retry_attempt.CredentialRetry.from_event(
        {
            "event": {
                **base,
                "probe": {
                    "attempt": 0,
                    "exec_id": "run.infra.0",
                    "submitted_at": 1.0,
                },
            }
        }
    ).resubmit_state()
    from_catch = persist_retry_attempt.CredentialRetry.from_event(
        {
            "event": {key: value for key, value in base.items() if key != "result"},
            "execution_started_at": "2026-01-01T00:00:00Z",
        }
    ).resubmit_state()
    for retried in (from_probe, from_catch):
        assert retried["folder_pin"]["source_run_id"] == "plan-run"
        assert retried["source_plan_run_id"] == "plan-run"
        assert retried["attempt"] == 1


def test_safe_collect_lane_omits_source_plan_run_id():
    state = {
        **_plan_map_item(),
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "probe": {
            "exec_id": "run.infra.0",
            "attempt": 0,
            "succeeded": True,
            "credential_expired": False,
            "steps": [],
            "error": None,
            "pointers": {"done": "s3://done/run.infra.0/done"},
            "submitted_at": 1.0,
        },
    }
    params = collect_task_parameters(state, mutation=False)
    assert "source_plan_run_id" not in params


def test_mutation_collect_lane_requires_source_plan_run_id():
    state = {
        **_plan_map_item(),
        "action": "apply",
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "source_plan_run_id": "plan-run",
        "probe": {
            "exec_id": "run.infra.0",
            "attempt": 0,
            "succeeded": True,
            "credential_expired": False,
            "steps": [],
            "error": None,
            "pointers": {"done": "s3://done/run.infra.0/done"},
            "submitted_at": 1.0,
        },
    }
    params = collect_task_parameters(state, mutation=True)
    assert params["source_plan_run_id"] == "plan-run"


def test_intent_create_lambda_receives_plan_retention_days():
    source = Path("infra/deploy/modules/openci_tf/lambdas.tf").read_text(encoding="utf-8")
    assert 'contains(["api", "render-pr", "intent-create"], each.key)' in source
    assert "PLAN_RETENTION_DAYS" in source


def test_failed_mutation_manifest_requires_source_plan_run_id():
    manifest = build_failure_manifest(
        execution_id="run.infra.0",
        tmp_bucket="tmp",
        done_bucket="done",
        package_bucket="pkg",
        action="apply",
        failure_reason="engine failed",
        run_id="run",
        repo_name="org/repo",
        commit_hash="a" * 40,
        account_id="123456789012",
        folder="infra",
        attempt=0,
        generated_at_source=__import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
        ),
        source_plan_run_id="plan-run",
    )
    validate_manifest_schema(manifest, execution_id="run.infra.0")
    with pytest.raises(ValueError, match="source_plan_run_id"):
        validate_manifest_schema(
            {**manifest, "source_plan_run_id": ""}, execution_id="run.infra.0"
        )


def test_plan_lookup_destroy_reads_destroy_pointer(monkeypatch):
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    captured: dict[str, object] = {}

    def fake_get_object_bytes(
        _bucket: str, key: str, *, max_bytes: int
    ) -> bytes | None:
        captured["pointer_key"] = key
        captured["max_bytes"] = max_bytes
        return b"EXECUTION_ID=1786850065992.24d545e0\n"

    def fake_sha256(
        run_id: str,
        folder: str,
        artifact_name: str,
        *,
        required_plan_action: str,
        commit_hash: str,
        account_id: str,
        expected_tf_runtime: str,
        repo_name: str = "",
        pr_number: int | None = None,
    ) -> str | None:
        captured["run_id"] = run_id
        captured["folder"] = folder
        captured["artifact_name"] = artifact_name
        captured["required_plan_action"] = required_plan_action
        captured["pr_number"] = pr_number
        return "d" * 64

    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_object_bytes", fake_get_object_bytes
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup._folder_plan_sha256", fake_sha256
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.list_runs_for_repo",
        lambda *_args, **_kwargs: pytest.fail(
            "pointer match should avoid fallback scan"
        ),
    )

    result = find_newest_fresh_plan_run(
        trigger_id="trigger",
        repo_name="org/repo",
        pr_number=7,
        folder="infra/ec2",
        mutation_action="destroy",
        commit_hash="a" * 40,
        account_id="222222222222",
        expected_tf_runtime="tofu:1.8.0",
    )

    assert result == {
        "run_id": "1786850065992.24d545e0",
        "folder": "infra/ec2",
        "plan_sha256": "d" * 64,
        "plan_artifact_name": "destroy.plan.tfplan",
        "tf_runtime": "tofu:1.8.0",
    }
    assert captured["pointer_key"] == pr_pointer_key(
        repo_name="org/repo",
        pr_number=7,
        folder_path="infra/ec2",
        pointer_type="destroy",
    )
    assert captured["max_bytes"] == 128
    assert captured["run_id"] == "1786850065992.24d545e0"
    assert captured["artifact_name"] == "destroy.plan.tfplan"
    assert captured["required_plan_action"] == "plan_destroy"
    assert captured["pr_number"] == 7


def test_plan_lookup_fallback_uses_pr_scoped_manifest_for_destroy(monkeypatch):
    captured: dict[str, object] = {}

    def fake_sha256(
        run_id: str,
        folder: str,
        artifact_name: str,
        *,
        required_plan_action: str,
        commit_hash: str,
        account_id: str,
        expected_tf_runtime: str,
        repo_name: str = "",
        pr_number: int | None = None,
    ) -> str | None:
        captured["run_id"] = run_id
        captured["folder"] = folder
        captured["artifact_name"] = artifact_name
        captured["required_plan_action"] = required_plan_action
        captured["commit_hash"] = commit_hash
        captured["account_id"] = account_id
        captured["expected_tf_runtime"] = expected_tf_runtime
        captured["repo_name"] = repo_name
        captured["pr_number"] = pr_number
        return "e" * 64 if pr_number == 7 else None

    monkeypatch.setattr(
        "src.domain.intent.plan_lookup._plan_run_from_pointer", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup._folder_plan_sha256", fake_sha256
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.list_runs_for_repo",
        lambda *_args, **_kwargs: (
            [
                {
                    "status": "succeeded",
                    "action": "plan_destroy",
                    "commit_hash": "a" * 40,
                    "notification_target": {"type": "github_pr", "pr_number": 7},
                    "run_id": "destroy-run",
                }
            ],
            None,
        ),
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_folder_record",
        lambda *_args, **_kwargs: {"status": "succeeded", "manifest_sha256": "c" * 64},
    )

    result = find_newest_fresh_plan_run(
        trigger_id="trigger",
        repo_name="org/repo",
        pr_number=7,
        folder="infra/ec2",
        mutation_action="destroy",
        commit_hash="a" * 40,
        account_id="222222222222",
        expected_tf_runtime="tofu:1.8.0",
    )

    assert result == {
        "run_id": "destroy-run",
        "folder": "infra/ec2",
        "plan_sha256": "e" * 64,
        "plan_artifact_name": "destroy.plan.tfplan",
        "tf_runtime": "tofu:1.8.0",
    }
    assert captured == {
        "run_id": "destroy-run",
        "folder": "infra/ec2",
        "artifact_name": "destroy.plan.tfplan",
        "required_plan_action": "plan_destroy",
        "commit_hash": "a" * 40,
        "account_id": "222222222222",
        "expected_tf_runtime": "tofu:1.8.0",
        "repo_name": "org/repo",
        "pr_number": 7,
    }


def test_plan_lookup_rejects_source_plan_under_different_account(monkeypatch):
    run_id = "plan-run"
    captured: dict[str, str] = {}

    def fake_sha256(
        _run_id: str,
        _folder: str,
        _artifact_name: str,
        *,
        required_plan_action: str,
        commit_hash: str,
        account_id: str,
        expected_tf_runtime: str,
        repo_name: str = "",
        pr_number: int | None = None,
    ) -> str | None:
        captured["account_id"] = account_id
        captured["required_plan_action"] = required_plan_action
        captured["commit_hash"] = commit_hash
        return None if account_id == "222222222222" else "b" * 64

    monkeypatch.setattr(
        "src.domain.intent.plan_lookup._folder_plan_sha256", fake_sha256
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.list_runs_for_repo",
        lambda *_args, **_kwargs: (
            [
                {
                    "status": "succeeded",
                    "action": "plan",
                    "commit_hash": "a" * 40,
                    "notification_target": {"type": "github_pr", "pr_number": 7},
                    "run_id": run_id,
                }
            ],
            None,
        ),
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_folder_record",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "account_id": "111111111111",
            "manifest_sha256": "c" * 64,
        },
    )
    assert (
        find_newest_fresh_plan_run(
            trigger_id="trigger",
            repo_name="org/repo",
            pr_number=7,
            folder="infra/vpc",
            mutation_action="apply",
            commit_hash="a" * 40,
            account_id="222222222222",
            expected_tf_runtime="tofu:1.8.0",
        )
        is None
    )
    assert captured["account_id"] == "222222222222"


def test_prepare_and_submit_rejects_folder_pin_account_mismatch():
    folder_pin = {
        "source_run_id": "plan-run",
        "plan_sha256": "b" * 64,
        "plan_artifact_name": "plan.tfplan",
        "account_id": "111111111111",
        "tf_runtime": "tofu:1.8.0",
    }
    with pytest.raises(ValueError, match="account_id does not match"):
        prepare_and_submit._validate_folder_pin(
            folder_pin,
            account_id="222222222222",
            tf_runtime="tofu:1.8.0",
        )


def test_intent_gate_pins_current_account_and_runtime(monkeypatch):
    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda _: SimpleNamespace(
            account_id="222222222222",
            role_name="openci-tf-executor-readonly",
            poweruser_role_name="openci-tf-executor-poweruser",
            external_id="openci-tf-0123456789abcdef",
            max_ttl=3600,
            enable_apply=True,
        ),
    )
    monkeypatch.setattr(
        "src.domain.intent.gates.find_newest_fresh_plan_run",
        lambda **_kwargs: {
            "run_id": "plan-run",
            "plan_sha256": "b" * 64,
            "plan_artifact_name": "plan.tfplan",
            "tf_runtime": "tofu:1.8.0",
        },
    )
    config = FolderConfig(
        account_alias="target",
        apply=MutationVerbConfig(allow=True),
        tf_runtime="tofu:1.8.0",
    )
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc"],
        folder_configs={"infra/vpc": config},
        settings=RepoSettings(
            trigger_id="t", repo_name="o/r", git_url="https://github.com/o/r"
        ),
        pr_number=1,
        commit_hash="a" * 40,
        approval_client=Mock(pr_has_approved_review=Mock(return_value=True)),
    )
    assert result.ok is True
    assert result.record is not None
    pin = result.record.folder_pins[0]
    assert pin.account_id == "222222222222"
    assert pin.tf_runtime == "tofu:1.8.0"


def test_typed_safe_credential_retry_preserves_inputs_without_mutation_pins():
    base_state = {
        **_plan_map_item(),
        **_shared_map_context(),
        "result": {
            "attempt": 0,
            "exec_id": "run.infra.0",
            "submitted_at": 1.0,
            "succeeded": False,
            "credential_expired": True,
        },
    }
    for action in ("plan", "plan_destroy", "drift", "report"):
        retry = persist_retry_attempt.CredentialRetry.from_event(
            {"event": {**base_state, "action": action}}
        )
        manifest_event = retry.manifest_event()
        resubmit = retry.resubmit_state()
        assert manifest_event["registry_only"] is True
        assert manifest_event["credential_expired"] is True
        assert resubmit["attempt"] == 1
        assert resubmit["git_url"] == base_state["git_url"]
        assert "result" not in resubmit
        assert "folder_pin" not in resubmit
        assert "source_plan_run_id" not in resubmit


def test_credential_retry_registry_only_skips_canonical_manifest_before_attempt_one_collect(
    monkeypatch,
):
    from src.domain.engine.execution_id import compose_execution_id

    run_id = "r" * 32
    folder = "infra/a"
    exec_id_attempt0 = compose_execution_id(run_id, folder, 0)
    exec_id_attempt1 = compose_execution_id(run_id, folder, 1)
    s3_writes: list[str] = []
    registry_calls: list[dict] = []

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "pkg")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        write_failure_manifest,
        "put_json_create_only",
        lambda _bucket, key, _manifest: s3_writes.append(key),
    )
    monkeypatch.setattr(
        persist_retry_attempt,
        "put_folder_attempt",
        lambda **kwargs: registry_calls.append(kwargs),
    )
    monkeypatch.setattr(
        write_failure_manifest,
        "put_folder_attempt",
        lambda **kwargs: registry_calls.append(kwargs),
    )

    retry_event = {
        "run_id": run_id,
        "folder": folder,
        "action": "plan",
        "account_id": "123456789012",
        "attempt": 0,
        "exec_id": exec_id_attempt0,
        "repo_name": "org/repo",
        "commit_hash": "a" * 40,
        "submitted_at": 1_700_000_000.0,
        "credential_expired": True,
        "failure_reason": "credential expired before retry",
        "registry_only": True,
        "result": {"attempt": 0, "exec_id": exec_id_attempt0},
    }
    resubmit = persist_retry_attempt.handler(
        {"event": retry_event, "execution_started_at": "2026-01-01T00:00:00Z"},
        object(),
    )
    assert s3_writes == []
    assert len(registry_calls) == 1
    assert registry_calls[0]["attempt"] == 0
    assert registry_calls[0]["outcome"]["credential_expired"] is True
    assert registry_calls[0]["manifest_s3_uri"] is None
    assert registry_calls[0]["manifest_sha256"] is None
    assert resubmit["attempt"] == 1
    assert "result" not in resubmit

    last_modified = __import__("datetime").datetime(
        2026, 8, 10, 12, 0, tzinfo=__import__("datetime").timezone.utc
    )
    plan_metadata, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id_attempt1,
        repo_name="org/repo",
        run_id=run_id,
        commit_hash="a" * 40,
        account_id="123456789012",
        folder=folder,
        attempt=1,
        last_modified=last_modified,
    )
    monkeypatch.setattr(collect, "head_object", head_object)
    monkeypatch.setattr(collect, "get_object_bytes", read_object_bytes)
    monkeypatch.setattr(
        collect,
        "put_json_create_only",
        lambda _bucket, key, _manifest: s3_writes.append(key),
    )
    monkeypatch.setattr(collect, "copy_object", lambda **_kwargs: None)
    monkeypatch.setattr(collect, "publish_execution_pointer", lambda **_kwargs: None)
    monkeypatch.setattr(
        collect, "put_folder_attempt", lambda **kwargs: registry_calls.append(kwargs)
    )

    def fake_get_bounded_json(_bucket, key, _limit):
        if key.endswith("manifest.json"):
            return None
        return plan_metadata

    monkeypatch.setattr(collect, "get_bounded_json", fake_get_bounded_json)
    summary = collect.handler(
        {
            "exec_id": exec_id_attempt1,
            "attempt": 1,
            "succeeded": True,
            "credential_expired": False,
            "steps": [],
            "error": None,
            "pointers": {"done": f"s3://done/{exec_id_attempt1}/done"},
            "action": "plan",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "account_id": "123456789012",
            "folder": folder,
            "run_id": run_id,
            "submitted_at": 1_700_000_010.0,
            "plan_metadata_uri": plan_metadata["metadata_s3_uri"],
        },
        object(),
    )
    assert summary["succeeded"] is True
    assert len(s3_writes) == 1
    assert s3_writes[0] == manifest_key("org/repo", run_id, folder)
    assert registry_calls[-1]["attempt"] == 1
    assert registry_calls[-1]["status"] == "succeeded"


def test_plan_lookup_rejects_tampered_source_metadata_and_runtime_mismatch(monkeypatch):
    from src.domain.engine.execution_id import compose_execution_id

    run_id = "r" * 32
    folder = "infra/vpc"
    commit_hash = "a" * 40
    account_id = "111111111111"
    exec_id = compose_execution_id(run_id, folder, 0)
    plan_body = b"committed-plan-bytes"
    committed_metadata = plan_metadata_dict(
        bucket="tmp",
        repo_name="org/repo",
        run_id=run_id,
        commit_hash=commit_hash,
        account_id=account_id,
        folder=folder,
        plan_body=plan_body,
    )
    committed_metadata["opentofu_runtime"] = "terraform:0.14.0"
    committed_metadata["created_at"] = "2099-08-10T00:00:00Z"
    committed_metadata["expires_at"] = "2099-08-11T00:00:00Z"
    committed_metadata_body = json.dumps(
        committed_metadata, separators=(",", ":")
    ).encode()
    tampered_metadata = {**committed_metadata, "opentofu_runtime": "tofu:1.8.0"}
    tampered_metadata_body = json.dumps(
        tampered_metadata, separators=(",", ":")
    ).encode()
    manifest = committed_success_plan_manifest(
        execution_id=exec_id,
        repo_name="org/repo",
        run_id=run_id,
        commit_hash=commit_hash,
        account_id=account_id,
        folder=folder,
    )
    future_expires = "2099-08-12T00:00:00Z"
    for entry in manifest["entries"]:
        entry["expires_at"] = future_expires
    plan_checksum = hashlib.sha256(plan_body).hexdigest()
    for entry in manifest["entries"]:
        if entry["name"] == "plan.tfplan":
            entry["checksum"] = plan_checksum
            entry["size"] = len(plan_body)
        if entry["name"] == "plan-metadata.json":
            entry["checksum"] = hashlib.sha256(committed_metadata_body).hexdigest()
            entry["size"] = len(committed_metadata_body)
    from src.domain.engine.manifest import _canonical_manifest_digest

    manifest["manifest_sha256"] = _canonical_manifest_digest(manifest)

    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("PLAN_RETENTION_DAYS", "1")
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_folder_record",
        lambda *_args, **_kwargs: {
            "status": "succeeded",
            "account_id": account_id,
            "manifest_sha256": manifest["manifest_sha256"],
        },
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_run",
        lambda *_args, **_kwargs: {"repo_name": "org/repo"},
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_bounded_json",
        lambda _bucket, key, _limit: (
            manifest if key.endswith("manifest.json") else None
        ),
    )

    _, head_object, read_object_bytes = complete_plan_object_mocks(
        execution_id=exec_id,
        repo_name="org/repo",
        run_id=run_id,
        commit_hash=commit_hash,
        account_id=account_id,
        folder=folder,
        attempt=0,
        last_modified=__import__("datetime").datetime(
            2026, 8, 10, tzinfo=__import__("datetime").timezone.utc
        ),
        plan_body=plan_body,
    )

    def read_metadata_bytes(_bucket: str, key: str, max_bytes: int) -> bytes | None:
        if key.endswith("plan-metadata.json"):
            body = tampered_metadata_body
        else:
            body = read_object_bytes(_bucket, key, max_bytes)
            if body is None:
                return None
        if len(body) > max_bytes:
            return None
        return body

    def head_with_metadata(_bucket: str, key: str) -> dict[str, Any] | None:
        if key.endswith("plan-metadata.json"):
            return {
                "content_length": len(tampered_metadata_body),
                "content_type": "application/json",
                "last_modified": __import__("datetime").datetime(
                    2026, 8, 10, tzinfo=__import__("datetime").timezone.utc
                ),
            }
        return head_object(_bucket, key)

    monkeypatch.setattr("src.domain.intent.plan_lookup.head_object", head_with_metadata)
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_object_bytes", read_metadata_bytes
    )

    assert (
        _folder_plan_sha256(
            run_id,
            folder,
            "plan.tfplan",
            required_plan_action="plan",
            commit_hash=commit_hash,
            account_id=account_id,
            expected_tf_runtime="tofu:1.8.0",
            repo_name="org/repo",
        )
        is None
    )

    def read_committed_metadata_bytes(
        _bucket: str, key: str, max_bytes: int
    ) -> bytes | None:
        if key.endswith("plan-metadata.json"):
            body = committed_metadata_body
        else:
            body = read_object_bytes(_bucket, key, max_bytes)
            if body is None:
                return None
        if len(body) > max_bytes:
            return None
        return body

    def head_with_committed_metadata(_bucket: str, key: str) -> dict[str, Any] | None:
        if key.endswith("plan-metadata.json"):
            return {
                "content_length": len(committed_metadata_body),
                "content_type": "application/json",
                "last_modified": __import__("datetime").datetime(
                    2026, 8, 10, tzinfo=__import__("datetime").timezone.utc
                ),
            }
        return head_object(_bucket, key)

    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.head_object", head_with_committed_metadata
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.get_object_bytes", read_committed_metadata_bytes
    )

    assert (
        _folder_plan_sha256(
            run_id,
            folder,
            "plan.tfplan",
            required_plan_action="plan",
            commit_hash=commit_hash,
            account_id=account_id,
            expected_tf_runtime="tofu:1.8.0",
            repo_name="org/repo",
        )
        is None
    )
    assert (
        _folder_plan_sha256(
            run_id,
            folder,
            "plan.tfplan",
            required_plan_action="plan",
            commit_hash=commit_hash,
            account_id=account_id,
            expected_tf_runtime="terraform:0.14.0",
            repo_name="org/repo",
        )
        == plan_checksum
    )
