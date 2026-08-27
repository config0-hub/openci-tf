# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply/destroy grammar, intent gates, and script generation tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from botocore.exceptions import EndpointConnectionError

from src.core.models import FolderConfig, MutationVerbConfig, RepoSettings
from src.core.errors import ConfigValidationError
from src.domain.cmd_builder.cmd_resolver import resolve_commands
from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.command.grammar import parse_command
from src.domain.config.folder_config import (
    compact_folder_config_for_outer_state,
    parse_folder_config,
)
from src.domain.config.pipeline import Pipeline, Step
from src.domain.intent.plan_lookup import PlanLookupResult
from src.domain.intent.gates import evaluate_confirm_gates, evaluate_intent_gates
from src.domain.intent.models import FolderPlanPin, IntentGateFailure, IntentRecord
from src.domain.intent.token import mint_token
from src.services.intent.confirm import confirm_intent
from src.services.intent.create import IntentCreationError, create_intent
from src.services.intent import handler as intent_handler
from src.services.intent.handler import confirm_handler, create_handler
from src.services.intent.registry import mark_intent_used, put_intent, get_intent
from src.platform.aws.run_registry import RunRegistryError
from src.services.render.handler import _pipeline_apply_footer


@pytest.mark.parametrize(
    "text,action,folders,destroy,token",
    [
        ("tf plan --destroy infra/vpc", "plan", ["infra/vpc"], True, None),
        (
            "tf apply infra/vpc,infra/rds",
            "apply",
            ["infra/vpc", "infra/rds"],
            False,
            None,
        ),
        ("tf apply confirm abc123", "apply", [], False, "abc123"),
        ("tf destroy infra/vpc", "destroy", ["infra/vpc"], False, None),
        ("tf destroy confirm deadbeef", "destroy", [], False, "deadbeef"),
    ],
)
def test_apply_destroy_grammar(text, action, folders, destroy, token):
    cmd = parse_command(text)
    assert cmd.action == action
    assert cmd.folders == folders
    assert cmd.destroy_flag is destroy
    assert cmd.confirm_token == token


def test_folder_config_apply_destroy_default_false():
    config = parse_folder_config("account_alias: target\n")
    assert config.apply.allow is False
    assert config.destroy.allow is False


def test_folder_config_apply_destroy_explicit_true_rejected():
    with pytest.raises(ConfigValidationError, match="mapping with allow/grace_seconds"):
        parse_folder_config("account_alias: target\napply: true\ndestroy: true\n")


def test_folder_config_block_syntax_with_grace():
    config = parse_folder_config(
        "account_alias: target\n"
        "apply:\n  allow: true\n  grace_seconds: 30\n"
        "destroy:\n  allow: true\n"
    )
    assert config.apply.allow is True
    assert config.apply.grace_seconds == 30
    assert config.destroy.allow is True
    assert config.destroy.grace_seconds == 60


def test_compact_enabled_mutation_blocks_retain_default_grace_seconds():
    config = compact_folder_config_for_outer_state(
        {
            "account_alias": "target",
            "apply": {"allow": True, "grace_seconds": 15},
            "destroy": {"allow": True, "grace_seconds": 60},
        }
    )

    assert config["apply"] == {"allow": True, "grace_seconds": 15}
    assert config["destroy"] == {"allow": True, "grace_seconds": 60}
    assert FolderConfig(**config).resolved_grace_seconds("destroy") == 60


def test_apply_and_destroy_scripts_verify_sha256_and_apply_plan():
    for verb, artifact in (
        ("apply", "plan.tfplan"),
        ("destroy", "destroy.plan.tfplan"),
    ):
        script = render(ScriptParams(verb=verb, execution_target="lambda"))
        assert "download_and_verify_pinned_plan" in script
        assert "OPENCI_TF_PINNED_PLAN_SHA256" in script
        assert f'"{artifact}"' in script
        assert "tofu show" in script
        assert script.index("tofu show") < script.index("tofu apply")
        assert "tofu apply -no-color" in script


def test_plan_destroy_script_writes_destroy_plan_artifacts():
    script = render(ScriptParams(verb="plan_destroy", execution_target="lambda"))
    assert "destroy.plan.tfplan" in script
    assert "upload_destroy_plan_binary_artifact" in script
    assert "-destroy -out=" in script


def test_collect_accepts_destroy_plan_metadata_key_for_plan_destroy():
    from src.domain.engine.artifact_paths import build_folder_artifact_keys
    from src.services.run_folder.collect import _expected_plan_metadata_key

    keys = build_folder_artifact_keys(
        repo_name="org/repo", run_id="run-1", folder_path="infra/a"
    )

    assert _expected_plan_metadata_key("plan", keys) == keys.plan_metadata
    assert _expected_plan_metadata_key("report", keys) == keys.plan_metadata
    assert _expected_plan_metadata_key("plan_destroy", keys) == keys.destroy_plan_metadata


def test_cross_application_refusal_in_pinned_plan_helper():
    script = render(ScriptParams(verb="apply", execution_target="lambda"))
    assert "plan.tfplan" in script
    assert "destroy.plan.tfplan" in script
    assert "unsupported pinned plan artifact" in script


def test_intent_gate_apply_disabled_for_account(monkeypatch):
    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda _: SimpleNamespace(account_id="123456789012", enable_apply=False),
    )
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc"],
        folder_configs={
            "infra/vpc": FolderConfig(
                account_alias="target", apply=MutationVerbConfig(allow=True)
            )
        },
        settings=RepoSettings(
            trigger_id="t", repo_name="o/r", git_url="https://github.com/o/r"
        ),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is False
    assert (
        result.failures[0].message
        == "apply/destroy not enabled for account target (123456789012)"
    )


def test_intent_gate_apply_enabled_account_passes_folder_gate(monkeypatch):
    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda alias: SimpleNamespace(
            account_id="123456789012" if alias == "target" else "210987654321",
            role_name="openci-tf-executor-readonly",
            poweruser_role_name="openci-tf-executor-poweruser",
            external_id="openci-tf-0123456789abcdef",
            max_ttl=3600,
            enable_apply=True,
        ),
    )
    monkeypatch.setattr(
        "src.domain.intent.gates.find_newest_fresh_plan_run",
        lambda **_kwargs: PlanLookupResult(
            match={
                "run_id": "plan-run",
                "plan_sha256": "b" * 64,
                "plan_artifact_name": "plan.tfplan",
                "tf_runtime": "tofu:1.8.0",
            }
        ),
    )
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc"],
        folder_configs={
            "infra/vpc": FolderConfig(
                account_alias="target", apply=MutationVerbConfig(allow=True)
            )
        },
        settings=RepoSettings(
            trigger_id="t", repo_name="o/r", git_url="https://github.com/o/r"
        ),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is True


def test_intent_gate_mixed_accounts_collects_all_enable_apply_failures(monkeypatch):
    def _load(alias: str):
        accounts = {
            "prod": SimpleNamespace(account_id="123456789012", enable_apply=False),
            "staging": SimpleNamespace(account_id="210987654321", enable_apply=False),
        }
        return accounts[alias]

    monkeypatch.setattr("src.domain.intent.gates.load_account_alias", _load)
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc", "infra/rds"],
        folder_configs={
            "infra/vpc": FolderConfig(
                account_alias="prod", apply=MutationVerbConfig(allow=True)
            ),
            "infra/rds": FolderConfig(
                account_alias="staging", apply=MutationVerbConfig(allow=True)
            ),
        },
        settings=RepoSettings(
            trigger_id="t", repo_name="o/r", git_url="https://github.com/o/r"
        ),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is False
    assert len(result.failures) == 2
    messages = {failure.message for failure in result.failures}
    assert messages == {
        "apply/destroy not enabled for account prod (123456789012)",
        "apply/destroy not enabled for account staging (210987654321)",
    }


def test_intent_gate_folder_not_enabled(monkeypatch):
    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda _: SimpleNamespace(account_id="123456789012", enable_apply=True),
    )
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc"],
        folder_configs={"infra/vpc": FolderConfig(account_alias="target")},
        settings=RepoSettings(
            trigger_id="t", repo_name="o/r", git_url="https://github.com/o/r"
        ),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is False
    assert "apply.allow: true" in result.failures[0].message


def test_intent_gate_refuses_stale_apply_plan_after_successful_mutation(monkeypatch):
    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda _: SimpleNamespace(account_id="123456789012", enable_apply=True),
    )

    def fake_lookup(**_kwargs):
        return PlanLookupResult(match=None, stale=True)

    monkeypatch.setattr("src.domain.intent.gates.find_newest_fresh_plan_run", fake_lookup)
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc"],
        folder_configs={
            "infra/vpc": FolderConfig(
                account_alias="target", apply=MutationVerbConfig(allow=True)
            )
        },
        settings=RepoSettings(
            trigger_id="t", repo_name="o/r", git_url="https://github.com/o/r"
        ),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is False
    assert result.failures[0].message == "stale plan — re-run tf plan infra/vpc"


def test_intent_gate_refuses_stale_destroy_plan_after_successful_mutation(monkeypatch):
    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda _: SimpleNamespace(account_id="123456789012", enable_apply=True),
    )

    def fake_lookup(**_kwargs):
        return PlanLookupResult(match=None, stale=True)

    monkeypatch.setattr("src.domain.intent.gates.find_newest_fresh_plan_run", fake_lookup)
    result = evaluate_intent_gates(
        action="destroy",
        folders=["infra/vpc"],
        folder_configs={
            "infra/vpc": FolderConfig(
                account_alias="target", destroy=MutationVerbConfig(allow=True)
            )
        },
        settings=RepoSettings(
            trigger_id="t", repo_name="o/r", git_url="https://github.com/o/r"
        ),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is False
    assert (
        result.failures[0].message
        == "stale plan — re-run tf plan --destroy infra/vpc"
    )


def test_plan_lookup_marks_plan_stale_when_newer_mutation_succeeded(monkeypatch):
    from src.domain.intent.plan_lookup import find_newest_fresh_plan_run

    monkeypatch.setattr(
        "src.domain.intent.plan_lookup._plan_run_from_pointer", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup._folder_plan_sha256",
        lambda *_args, **_kwargs: "c" * 64,
    )
    monkeypatch.setattr(
        "src.domain.intent.plan_lookup.list_runs_for_repo",
        lambda *_args, **_kwargs: (
            [
                {
                    "status": "succeeded",
                    "action": "plan_destroy",
                    "commit_hash": "a" * 40,
                    "notification_target": {"type": "github_pr", "pr_number": 1},
                    "run_id": "1700000000000.aaaaaaaa",
                },
                {
                    "status": "succeeded",
                    "action": "destroy",
                    "commit_hash": "a" * 40,
                    "notification_target": {"type": "github_pr", "pr_number": 1},
                    "run_id": "1700000001000.bbbbbbbb",
                },
            ],
            None,
        ),
    )

    def folder_record(run_id: str, folder: str):
        if run_id == "1700000001000.bbbbbbbb" and folder == "infra/vpc":
            return {"status": "succeeded"}
        if run_id == "1700000000000.aaaaaaaa":
            return {"status": "succeeded", "manifest_sha256": "d" * 64}
        return None

    monkeypatch.setattr("src.domain.intent.plan_lookup.get_folder_record", folder_record)

    result = find_newest_fresh_plan_run(
        trigger_id="trigger",
        repo_name="o/r",
        pr_number=1,
        folder="infra/vpc",
        mutation_action="destroy",
        commit_hash="a" * 40,
        account_id="123456789012",
        expected_tf_runtime="tofu:1.8.0",
    )
    assert result.match is None
    assert result.stale is True


def _intent_gate_settings() -> RepoSettings:
    return RepoSettings(
        trigger_id="t",
        repo_name="o/r",
        git_url="https://github.com/o/r",
        ssm_openci_tf_github_token="/openci-tf/clone-token/test",
    )


def _phase4_pipeline() -> Pipeline:
    return Pipeline(
        name="data/primary",
        steps=(
            Step(("infra/vpc",)),
            Step(("infra/rds", "infra/ec2")),
            Step(("infra/db",)),
        ),
    )


def _phase4_configs() -> dict[str, FolderConfig]:
    return {
        folder: FolderConfig(account_alias="target", apply=MutationVerbConfig(allow=True))
        for folder in ["infra/vpc", "infra/rds", "infra/ec2", "infra/db"]
    }


def _phase4_record(folders: list[str]) -> IntentRecord:
    return IntentRecord(
        token="abc123",
        trigger_id="t",
        pr_number=1,
        action="apply",
        source_run_id="plan-run",
        folders=tuple(folders),
        commit_hash="a" * 40,
        folder_pins=(),
        expires_at=9999999999,
    )


def test_apply_pipeline_intent_checks_all_pipeline_folders_before_step(monkeypatch):
    calls: list[list[str]] = []

    def _fake_gates(**kwargs):
        folders = list(kwargs["folders"])
        calls.append(folders)
        if "infra/db" in folders:
            return SimpleNamespace(
                ok=False,
                failures=[IntentGateFailure("db blocks apply", folder="infra/db")],
                record=None,
            )
        return SimpleNamespace(ok=True, failures=[], record=_phase4_record(folders))

    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "h" * 64),
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", _fake_gates)
    monkeypatch.setattr("src.services.intent.create.put_intent", lambda _record: pytest.fail("blocked pipeline must not store an intent"))

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=1,
    )

    assert record is None
    assert failure == IntentGateFailure("db blocks apply", folder="infra/db")
    assert calls == [["infra/vpc", "infra/rds", "infra/ec2", "infra/db"]]


def test_apply_pipeline_intent_scopes_record_to_requested_step(monkeypatch):
    stored: list[IntentRecord] = []

    def _fake_gates(**kwargs):
        folders = list(kwargs["folders"])
        return SimpleNamespace(ok=True, failures=[], record=_phase4_record(folders))

    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "c" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_apply",
        lambda **_kwargs: {"pipeline_sha256": "c" * 64},
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", _fake_gates)
    monkeypatch.setattr("src.services.intent.create.put_intent", lambda record: stored.append(record))

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=2,
    )

    assert failure is None
    assert record is not None
    assert record["folders"] == ["infra/rds", "infra/ec2"]
    assert record["pipeline"] == "data/primary"
    assert record["step_index"] == 2
    assert record["step_count"] == 3
    assert record["pipeline_sha256"] == "c" * 64
    assert stored[0].folders == ("infra/rds", "infra/ec2")


def _us08_two_step_pipeline() -> Pipeline:
    return Pipeline(
        name="primary-msg",
        steps=(
            Step(("terraform/primary/ap-northeast-1/03-sqs",)),
            Step(("terraform/primary/ap-northeast-1/06-sns-topic",)),
        ),
    )


def _us08_two_step_configs() -> dict[str, FolderConfig]:
    return {
        folder: FolderConfig(account_alias="target", apply=MutationVerbConfig(allow=True))
        for folder in [
            "terraform/primary/ap-northeast-1/03-sqs",
            "terraform/primary/ap-northeast-1/06-sns-topic",
        ]
    }


def test_pipeline_apply_step_2_succeeds_without_replan_after_step_1_apply(monkeypatch):
    step1 = "terraform/primary/ap-northeast-1/03-sqs"
    step2 = "terraform/primary/ap-northeast-1/06-sns-topic"
    plan_run_id = "1787688123671.7e34ddd6"
    stored: list[IntentRecord] = []

    def fake_lookup(**kwargs):
        folder = kwargs["folder"]
        if folder == step1:
            return PlanLookupResult(match=None, stale=True)
        if folder == step2:
            return PlanLookupResult(
                match={
                    "run_id": plan_run_id,
                    "folder": step2,
                    "plan_sha256": "a" * 64,
                    "plan_artifact_name": "plan.tfplan",
                    "tf_runtime": "terraform",
                }
            )
        return PlanLookupResult(match=None)

    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_us08_two_step_pipeline(), _us08_two_step_configs(), "p" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_apply",
        lambda **_kwargs: {"pipeline_sha256": "p" * 64},
    )
    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda alias: SimpleNamespace(
            account_id="123456789012",
            role_name="openci-tf-executor-readonly",
            poweruser_role_name="openci-tf-executor-poweruser",
            external_id="openci-tf-0123456789abcdef",
            max_ttl=3600,
            enable_apply=True,
        ),
    )
    monkeypatch.setattr("src.domain.intent.gates.find_newest_fresh_plan_run", fake_lookup)
    monkeypatch.setattr("src.services.intent.create.put_intent", lambda record: stored.append(record))

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="primary-msg",
        pipeline_step=2,
    )

    assert failure is None
    assert record is not None
    assert record["folders"] == [step2]
    assert record["source_run_id"] == plan_run_id
    assert stored[0].folders == (step2,)


def test_pipeline_apply_step_2_refused_when_step_1_not_applied(monkeypatch):
    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_us08_two_step_pipeline(), _us08_two_step_configs(), "p" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_apply",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.domain.intent.gates.find_newest_fresh_plan_run",
        lambda **_kwargs: pytest.fail("missing step 1 anchor must reject before plan lookup"),
    )

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="primary-msg",
        pipeline_step=2,
    )

    assert record is None
    assert failure is not None
    assert failure.message == "pipeline primary-msg step 2 requires a completed apply of step 1 first"


def test_apply_pipeline_intent_rejects_pipeline_hash_mismatch(monkeypatch):
    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "new"),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_apply",
        lambda **_kwargs: {"pipeline_sha256": "old"},
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", lambda **_kwargs: pytest.fail("hash mismatch must reject before gates"))

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=2,
    )

    assert record is None
    assert failure is not None
    assert failure.message == "pipeline data/primary changed since step 1 was applied; restart from step 1"


def test_apply_pipeline_intent_rejects_missing_prior_step_anchor(monkeypatch):
    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "h" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_apply",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", lambda **_kwargs: pytest.fail("missing anchor must reject before gates"))

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=3,
    )

    assert record is None
    assert failure is not None
    assert failure.message == "pipeline data/primary step 3 requires a completed apply of step 2 first"


def test_apply_pipeline_intent_propagates_prior_step_registry_errors(monkeypatch):
    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "h" * 64),
    )

    def _fail_registry(**_kwargs):
        raise RunRegistryError("registry query failed")

    monkeypatch.setattr("src.services.intent.create.find_latest_successful_pipeline_apply", _fail_registry)

    with pytest.raises(IntentCreationError, match="registry query failed"):
        create_intent(
            action="apply",
            folders=[],
            trigger_id="t",
            pr_number=1,
            commit_hash="a" * 40,
            pipeline="data/primary",
            pipeline_step=2,
        )


def test_apply_pipeline_intent_accepts_same_hash_prior_step_from_previous_day(monkeypatch):
    stored: list[IntentRecord] = []

    def _fake_gates(**kwargs):
        folders = list(kwargs["folders"])
        return SimpleNamespace(ok=True, failures=[], record=_phase4_record(folders))

    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "w" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_apply",
        lambda **_kwargs: {
            "pipeline_sha256": "w" * 64,
            "pipeline_apply_completed_at": 1_700_000_000 - 86_400,
        },
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", _fake_gates)
    monkeypatch.setattr("src.services.intent.create.put_intent", lambda record: stored.append(record))

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=2,
    )

    assert failure is None
    assert record is not None
    assert stored[0].step_index == 2


def test_apply_pipeline_intent_accepts_whitespace_only_pipeline_change(monkeypatch):
    stored: list[IntentRecord] = []

    def _fake_gates(**kwargs):
        folders = list(kwargs["folders"])
        return SimpleNamespace(ok=True, failures=[], record=_phase4_record(folders))

    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "canonical"),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_apply",
        lambda **_kwargs: {"pipeline_sha256": "canonical"},
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", _fake_gates)
    monkeypatch.setattr("src.services.intent.create.put_intent", lambda record: stored.append(record))

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=2,
    )

    assert failure is None
    assert record is not None
    assert stored[0].pipeline_sha256 == "canonical"


def test_apply_pipeline_intent_rejects_step_out_of_range(monkeypatch):
    monkeypatch.setattr("src.services.intent.create.get_repo_settings", lambda *_args, **_kwargs: _intent_gate_settings())
    monkeypatch.setattr("src.services.intent.create.get_github_token", lambda _path: "github-token")
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_phase4_pipeline(), _phase4_configs(), "h" * 64),
    )

    failure, record = create_intent(
        action="apply",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=4,
    )

    assert record is None
    assert failure is not None
    assert "step_count=3" in failure.message


def test_pipeline_apply_footer_renders_next_step_and_completion():
    event = {
        "webhook_info": {
            "pipeline": "data/primary",
            "pipeline_step_index": 1,
            "pipeline_step_count": 2,
        }
    }
    outcomes = [{"folder": "infra/vpc", "status": "succeeded", "succeeded": True}]

    assert _pipeline_apply_footer(event, "apply", outcomes, []) == "next: tf apply pipeline data/primary step 2"

    complete_event = {
        "webhook_info": {
            "pipeline": "data/primary",
            "pipeline_step_index": 2,
            "pipeline_step_count": 2,
        }
    }
    assert _pipeline_apply_footer(complete_event, "apply", outcomes, []) == "pipeline data/primary complete (2 steps)"
    assert _pipeline_apply_footer(event, "apply", [{"status": "failed"}], []) is None


@patch("src.domain.accounts.aliases.get_account_alias")
def test_intent_gate_unknown_account_alias_refusal(get_account_alias):
    get_account_alias.side_effect = ValueError("Unknown account alias: 'missing'")
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc"],
        folder_configs={
            "infra/vpc": FolderConfig(
                account_alias="missing", apply=MutationVerbConfig(allow=True)
            )
        },
        settings=_intent_gate_settings(),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is False
    assert len(result.failures) == 1
    assert result.failures[0].folder == "infra/vpc"
    assert result.failures[0].message == (
        "account alias 'missing' is invalid or not registered: Unknown account alias: 'missing'"
    )


@patch("src.domain.accounts.aliases.get_account_alias")
def test_intent_gate_malformed_account_alias_refusal(get_account_alias):
    get_account_alias.return_value = {"account_id": "invalid", "role_name": "bad role!"}
    result = evaluate_intent_gates(
        action="apply",
        folders=["infra/vpc"],
        folder_configs={
            "infra/vpc": FolderConfig(
                account_alias="broken", apply=MutationVerbConfig(allow=True)
            )
        },
        settings=_intent_gate_settings(),
        pr_number=1,
        commit_hash="a" * 40,
    )
    assert result.ok is False
    assert result.failures[0].message == (
        "account alias 'broken' is invalid or not registered: account alias has invalid account_id"
    )


@patch("src.domain.accounts.aliases.get_account_alias")
def test_create_handler_unknown_account_alias_returns_intent_failed(
    get_account_alias, monkeypatch
):
    get_account_alias.side_effect = ValueError("Unknown account alias: 'missing'")
    monkeypatch.setattr(
        "src.services.intent.create._folder_configs_for_intent",
        lambda **_kwargs: {
            "infra/vpc": FolderConfig(
                account_alias="missing", apply=MutationVerbConfig(allow=True)
            )
        },
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_repo_settings",
        lambda *_args, **_kwargs: _intent_gate_settings(),
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_github_token",
        lambda _path: "github-token",
    )
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )

    event = {
        "action": "apply",
        "folders": ["infra/vpc"],
        "webhook_info": {"pr_number": 1, "trigger_id": "t", "repo_name": "o/r"},
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }
    result = create_handler(event, None)
    assert result["intent_failed"] is True
    assert result["intent_failures"] == [
        "account alias 'missing' is invalid or not registered: Unknown account alias: 'missing'"
    ]


@patch("src.domain.accounts.aliases.get_account_alias")
def test_create_handler_malformed_account_alias_returns_intent_failed(
    get_account_alias, monkeypatch
):
    get_account_alias.return_value = {"account_id": "invalid", "role_name": "bad role!"}
    monkeypatch.setattr(
        "src.services.intent.create._folder_configs_for_intent",
        lambda **_kwargs: {
            "infra/vpc": FolderConfig(
                account_alias="broken", apply=MutationVerbConfig(allow=True)
            )
        },
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_repo_settings",
        lambda *_args, **_kwargs: _intent_gate_settings(),
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_github_token",
        lambda _path: "github-token",
    )
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )

    event = {
        "action": "apply",
        "folders": ["infra/vpc"],
        "webhook_info": {"pr_number": 1, "trigger_id": "t", "repo_name": "o/r"},
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }
    result = create_handler(event, None)
    assert result["intent_failed"] is True
    assert result["intent_failures"] == [
        "account alias 'broken' is invalid or not registered: account alias has invalid account_id"
    ]


@patch("src.domain.accounts.aliases.get_account_alias")
def test_create_handler_failure_posts_context_before_deleting_command(
    get_account_alias, monkeypatch
):
    get_account_alias.side_effect = ValueError("Unknown account alias: 'missing'")
    monkeypatch.setattr(
        "src.services.intent.create._folder_configs_for_intent",
        lambda **_kwargs: {
            "infra/vpc": FolderConfig(
                account_alias="missing", apply=MutationVerbConfig(allow=True)
            )
        },
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_repo_settings",
        lambda *_args, **_kwargs: _intent_gate_settings(),
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_github_token",
        lambda _path: "github-token",
    )
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    posted: list[str] = []
    deleted: list[int | None] = []
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment",
        lambda _webhook, _settings, body: posted.append(body) or 9001,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_triggering_comment_after_replacement",
        lambda _webhook, _settings, comment_id: deleted.append(comment_id),
    )

    event = {
        "action": "apply",
        "folders": ["infra/vpc"],
        "webhook_info": {
            "pr_number": 1,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 44,
            "comment_body": "tf apply infra/vpc",
            "commit_hash": "a" * 40,
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = create_handler(event, None)

    assert result["intent_failed"] is True
    assert deleted == [44]
    assert len(posted) == 1
    assert "### openci-tf command" in posted[0]
    assert "- command: `tf apply infra/vpc`" in posted[0]
    assert "- triggering comment id: `44` (removed after acknowledgement)" in posted[0]
    assert "## tf apply refused" in posted[0]


def test_create_handler_success_keeps_requested_command_until_terminal_render(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        "src.services.intent.handler.create_intent",
        lambda **_kwargs: (
            None,
            {
                "token": "abc123",
                "trigger_id": "t",
                "pr_number": 1,
                "action": "apply",
                "source_run_id": "plan-run",
                "folders": ["infra/vpc"],
                "commit_hash": "a" * 40,
                "expires_at": 9999999999,
            },
        ),
    )
    monkeypatch.setattr(
        "src.services.intent.handler.store_intent_comment_metadata",
        lambda *_args, **_kwargs: None,
    )
    deleted: list[int | None] = []
    posted: list[str] = []
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment",
        lambda _webhook, _settings, body: posted.append(body) or 9001,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_triggering_comment_after_replacement",
        lambda _webhook, _settings, comment_id: deleted.append(comment_id),
    )

    event = {
        "action": "apply",
        "folders": ["infra/vpc"],
        "webhook_info": {
            "pr_number": 1,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 44,
            "comment_body": "tf apply infra/vpc",
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = create_handler(event, None)

    assert result["intent_created"] is True
    assert deleted == []
    assert (
        "- triggering comment: [44](https://github.com/o/r/pull/1#issuecomment-44)"
        in posted[0]
    )
    assert "cleanup deferred to terminal comment" in posted[0]
    assert "removed after acknowledgement" not in posted[0]


def test_intent_post_comment_bounds_large_command_context(monkeypatch):
    captured: list[str] = []

    class Client:
        def __init__(self, _token):
            pass

        def create_comment(self, _repo, _pr, body):
            captured.append(body)
            return 9001

    monkeypatch.setattr(intent_handler, "get_github_token", lambda _path: "token")
    monkeypatch.setattr(intent_handler, "GitHubClient", Client)
    huge_command = "tf " + (" " * 65_520) + "apply a"
    body = intent_handler._with_intent_command_context(
        {"repo_name": "o/r", "pr_number": 1, "comment_id": 44, "comment_body": huge_command},
        "apply",
        "## confirm",
    )

    comment_id = intent_handler._post_comment(
        {"repo_name": "o/r", "pr_number": 1},
        {"ssm_openci_tf_github_token": "/token"},
        body,
    )

    assert comment_id == 9001
    assert len(captured[0]) <= 65_536
    assert "tf apply a" in captured[0]


def test_create_handler_metadata_endpoint_failure_deletes_intent_comment_and_invalidates_token(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    live_tokens = {"abc123"}
    monkeypatch.setattr(
        "src.services.intent.handler.create_intent",
        lambda **_kwargs: (
            None,
            {
                "token": "abc123",
                "trigger_id": "t",
                "pr_number": 1,
                "action": "apply",
                "source_run_id": "plan-run",
                "folders": ["infra/vpc"],
                "commit_hash": "a" * 40,
                "expires_at": 9999999999,
            },
        ),
    )

    def fail_metadata(*_args, **_kwargs):
        raise EndpointConnectionError(endpoint_url="https://dynamodb.example.test")

    deleted_batches: list[list[int | None]] = []
    monkeypatch.setattr(
        "src.services.intent.handler.store_intent_comment_metadata",
        fail_metadata,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment",
        lambda *_args, **_kwargs: 9001,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_comments_after_replacement",
        lambda _webhook, _settings, comment_ids: deleted_batches.append(list(comment_ids)),
    )
    monkeypatch.setattr(
        "src.services.intent.handler.delete_intent",
        lambda token: live_tokens.discard(token),
    )

    event = {
        "action": "apply",
        "folders": ["infra/vpc"],
        "webhook_info": {
            "pr_number": 1,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 44,
            "comment_body": "tf apply infra/vpc",
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    with pytest.raises(EndpointConnectionError):
        create_handler(event, None)

    assert deleted_batches == [[9001]]
    assert "abc123" not in live_tokens


def test_create_handler_ambiguous_post_sweeps_bot_token_comment_and_invalidates_token(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    live_tokens = {"abc123"}
    comments = [
        {"id": 101, "body": "human says tf apply confirm abc123", "login": "alice"},
    ]
    deleted: list[int] = []
    monkeypatch.setattr(
        "src.services.intent.handler.create_intent",
        lambda **_kwargs: (
            None,
            {
                "token": "abc123",
                "trigger_id": "t",
                "pr_number": 1,
                "action": "apply",
                "source_run_id": "plan-run",
                "folders": ["infra/vpc"],
                "commit_hash": "a" * 40,
                "expires_at": 9999999999,
            },
        ),
    )
    monkeypatch.setattr(
        "src.services.intent.handler.store_intent_comment_metadata",
        lambda *_args, **_kwargs: pytest.fail("metadata must not run after ambiguous post"),
    )
    monkeypatch.setattr(intent_handler, "get_github_token", lambda _path: "token")

    class Client:
        def __init__(self, _token):
            pass

        def create_comment(self, _repo, _pr, body):
            comments.append({"id": 9001, "body": body, "login": "openci-bot"})
            raise requests.ConnectionError("post result unknown")

        def token_login(self):
            return "openci-bot"

        def find_comments_by_body_substring(self, _repo, _pr, needle):
            return [
                (comment["id"], comment["login"])
                for comment in comments
                if needle in comment["body"]
            ]

        def delete_comment(self, _repo, comment_id):
            deleted.append(comment_id)

    monkeypatch.setattr(intent_handler, "GitHubClient", Client)
    monkeypatch.setattr(
        "src.services.intent.handler.delete_intent",
        lambda token: live_tokens.discard(token),
    )

    event = {
        "action": "apply",
        "folders": ["infra/vpc"],
        "webhook_info": {
            "pr_number": 1,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 44,
            "comment_body": "tf apply infra/vpc",
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    with pytest.raises(requests.ConnectionError, match="post result unknown"):
        create_handler(event, None)

    assert deleted == [9001]
    assert "abc123" not in live_tokens


def test_confirm_handler_failure_deletes_confirmation_intent_and_requested_comments(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        "src.services.intent.handler.confirm_intent",
        lambda **_kwargs: ([IntentGateFailure("token already used")], None),
    )
    monkeypatch.setattr(
        "src.services.intent.handler.get_intent",
        lambda _token: IntentRecord(
            token="abc123",
            trigger_id="t",
            pr_number=1,
            action="apply",
            source_run_id="run1",
            folders=("infra/vpc",),
            commit_hash="a" * 40,
            folder_pins=(),
            expires_at=9999999999,
            requested_comment_id=10,
            intent_comment_id=11,
        ),
    )
    deleted_batches: list[list[int | None]] = []
    stale_tokens: list[str | None] = []
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment",
        lambda *_args, **_kwargs: 9001,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_comments_after_replacement",
        lambda _webhook, _settings, comment_ids: deleted_batches.append(list(comment_ids)),
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_stale_confirm_token_comments_after_replacement",
        lambda _webhook, _settings, token, **kwargs: stale_tokens.append(token),
    )

    event = {
        "action": "apply",
        "confirm_token": "abc123",
        "webhook_info": {
            "pr_number": 1,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 55,
            "comment_body": "tf apply confirm abc123",
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = confirm_handler(event, None)

    assert result["intent_failed"] is True
    assert deleted_batches == [[55, 11, 10]]
    assert stale_tokens == ["abc123"]


def test_confirm_handler_foreign_pr_token_deletes_only_current_confirmation(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        "src.services.intent.handler.confirm_intent",
        lambda **_kwargs: (
            [IntentGateFailure("confirmation token does not match this pull request")],
            None,
        ),
    )
    monkeypatch.setattr(
        "src.services.intent.handler.get_intent",
        lambda _token: IntentRecord(
            token="abc123",
            trigger_id="t",
            pr_number=1,
            action="apply",
            source_run_id="run1",
            folders=("infra/vpc",),
            commit_hash="a" * 40,
            folder_pins=(),
            expires_at=9999999999,
            requested_comment_id=10,
            intent_comment_id=11,
        ),
    )
    deleted_batches: list[list[int | None]] = []
    stale_tokens: list[str | None] = []
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment",
        lambda *_args, **_kwargs: 9001,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_comments_after_replacement",
        lambda _webhook, _settings, comment_ids: deleted_batches.append(list(comment_ids)),
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_stale_confirm_token_comments_after_replacement",
        lambda _webhook, _settings, token, **kwargs: stale_tokens.append(token),
    )

    event = {
        "action": "apply",
        "confirm_token": "abc123",
        "webhook_info": {
            "pr_number": 2,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 55,
            "comment_body": "tf apply confirm abc123",
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = confirm_handler(event, None)

    assert result["intent_failed"] is True
    assert deleted_batches == [[55]]
    assert stale_tokens == []


def test_confirm_handler_action_mismatch_deletes_only_current_confirmation(
    monkeypatch,
):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        "src.services.intent.handler.confirm_intent",
        lambda **_kwargs: (
            [IntentGateFailure("token is for tf apply, not tf destroy")],
            None,
        ),
    )
    monkeypatch.setattr(
        "src.services.intent.handler.get_intent",
        lambda _token: IntentRecord(
            token="abc123",
            trigger_id="t",
            pr_number=1,
            action="apply",
            source_run_id="run1",
            folders=("infra/vpc",),
            commit_hash="a" * 40,
            folder_pins=(),
            expires_at=9999999999,
            requested_comment_id=10,
            intent_comment_id=11,
        ),
    )
    deleted_batches: list[list[int | None]] = []
    stale_tokens: list[str | None] = []
    monkeypatch.setattr(
        "src.services.intent.handler._post_comment",
        lambda *_args, **_kwargs: 9001,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_comments_after_replacement",
        lambda _webhook, _settings, comment_ids: deleted_batches.append(list(comment_ids)),
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_stale_confirm_token_comments_after_replacement",
        lambda _webhook, _settings, token, **kwargs: stale_tokens.append(token),
    )

    event = {
        "action": "destroy",
        "confirm_token": "abc123",
        "webhook_info": {
            "pr_number": 1,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 55,
            "comment_body": "tf destroy confirm abc123",
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = confirm_handler(event, None)

    assert result["intent_failed"] is True
    assert deleted_batches == [[55]]
    assert stale_tokens == []


def test_confirm_handler_success_leaves_comments_for_terminal_render(monkeypatch):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha",
        lambda *_args, **_kwargs: "a" * 40,
    )
    monkeypatch.setattr(
        "src.services.intent.handler.confirm_intent",
        lambda **_kwargs: (
            [],
            {
                "action": "apply",
                "folders": ["infra/vpc"],
                "folder_pins": {"infra/vpc": {"source_run_id": "plan-run"}},
                "source_plan_run_id": "plan-run",
                "requested_comment_id": 10,
                "requested_comment_body": "tf apply infra/vpc",
                "intent_comment_id": 11,
                "intent_token": "abc123",
            },
        ),
    )
    deleted_batches: list[list[int | None]] = []
    stale_tokens: list[str | None] = []
    monkeypatch.setattr(
        "src.services.intent.handler._delete_comments_after_replacement",
        lambda _webhook, _settings, comment_ids: deleted_batches.append(list(comment_ids)),
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_stale_confirm_token_comments_after_replacement",
        lambda _webhook, _settings, token, **kwargs: stale_tokens.append(token),
    )

    event = {
        "action": "apply",
        "confirm_token": "abc123",
        "webhook_info": {
            "pr_number": 1,
            "trigger_id": "t",
            "repo_name": "o/r",
            "comment_id": 55,
            "comment_body": "tf apply confirm abc123",
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = confirm_handler(event, None)

    assert result["intent_confirmed"] is True
    assert result["consumed_confirm_token"] == "abc123"
    # Nothing is deleted on success: the terminal render removes the request,
    # intent, and confirmation comments after the apply/destroy comment exists.
    assert deleted_batches == []
    assert stale_tokens == []
    assert result["requested_comment_id"] == 10
    assert result["intent_comment_id"] == 11


def test_intent_record_comment_metadata_round_trips_through_registry(monkeypatch):
    stored: dict[str, object] = {}

    def _put(record: dict[str, object]) -> None:
        stored.update(record)

    def _get(token: str) -> dict[str, object] | None:
        if token != "abc123":
            return None
        return dict(stored)

    monkeypatch.setattr("src.services.intent.registry.put_intent_record", _put)
    monkeypatch.setattr("src.services.intent.registry.get_intent_record", _get)

    original = IntentRecord(
        token="abc123",
        trigger_id="t",
        pr_number=1,
        action="apply",
        source_run_id="run1",
        folders=("infra/vpc",),
        commit_hash="a" * 40,
        folder_pins=(),
        expires_at=9999999999,
        requested_comment_id=10,
        requested_comment_body="tf apply infra/vpc",
        intent_comment_id=11,
    )
    put_intent(original)
    loaded = get_intent("abc123")

    assert loaded is not None
    assert loaded.requested_comment_id == 10
    assert loaded.requested_comment_body == "tf apply infra/vpc"
    assert loaded.intent_comment_id == 11
    assert "requested_comment_id" in original.to_dict()
    assert "requested_comment_body" in original.to_dict()
    assert "intent_comment_id" in original.to_dict()


def test_confirm_handler_forces_pinned_intent_folder_selection(monkeypatch):
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha", lambda *_args: "a" * 40
    )
    monkeypatch.setattr(
        "src.services.intent.handler.confirm_intent",
        lambda **_kwargs: (
            [],
            {
                "action": "apply",
                "folders": ["infra/vpc"],
                "folder_pins": {"infra/vpc": {"source_run_id": "plan-run"}},
                "source_plan_run_id": "plan-run",
                "pipeline": "data/primary",
                "step_index": 1,
                "step_count": 2,
                "pipeline_sha256": "c" * 64,
            },
        ),
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_comments_after_replacement",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_stale_confirm_token_comments_after_replacement",
        lambda *_args, **_kwargs: None,
    )
    event = {
        "action": "apply",
        "folders": [],
        "all_flag": False,
        "affected_flag": True,
        "confirm_token": "abc123",
        "webhook_info": {"pr_number": 1, "trigger_id": "t", "repo_name": "o/r"},
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = confirm_handler(event, None)

    assert result["folders"] == ["infra/vpc"]
    assert result["affected_flag"] is False
    assert result["all_flag"] is False
    assert result["folder_pins"] == {"infra/vpc": {"source_run_id": "plan-run"}}
    assert result["webhook_info"]["pipeline"] == "data/primary"
    assert result["webhook_info"]["pipeline_step_index"] == 1
    assert result["webhook_info"]["pipeline_step_count"] == 2


def test_confirm_handler_records_pipeline_metadata_when_registry_enabled(monkeypatch):
    recorded: list[tuple[str, str, int]] = []
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(
        "src.services.intent.handler._current_pr_head_sha", lambda *_args: "a" * 40
    )
    monkeypatch.setattr(
        "src.services.intent.handler.confirm_intent",
        lambda **_kwargs: (
            [],
            {
                "action": "apply",
                "folders": ["infra/rds"],
                "folder_pins": {"infra/rds": {"source_run_id": "plan-run"}},
                "source_plan_run_id": "plan-run",
                "pipeline": "data/primary",
                "step_index": 2,
                "step_count": 3,
                "pipeline_sha256": "c" * 64,
            },
        ),
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_comments_after_replacement",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.services.intent.handler._delete_stale_confirm_token_comments_after_replacement",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.services.intent.handler.set_run_pipeline_metadata",
        lambda run_id, *, pipeline, step_count: recorded.append(
            (run_id, pipeline, step_count)
        ),
    )
    event = {
        "action": "apply",
        "folders": [],
        "all_flag": False,
        "affected_flag": True,
        "confirm_token": "abc123",
        "run_id": "run-1",
        "webhook_info": {"pr_number": 1, "trigger_id": "t", "repo_name": "o/r"},
        "settings": {"ssm_openci_tf_github_token": "/token"},
    }

    result = confirm_handler(event, None)

    assert recorded == [("run-1", "data/primary", 3)]
    assert result["webhook_info"]["pipeline"] == "data/primary"
    assert result["webhook_info"]["pipeline_step_index"] == 2
    assert result["webhook_info"]["pipeline_step_count"] == 3


def test_confirm_intent_carries_the_intents_frozen_account_binding(monkeypatch):
    frozen_binding = {
        "account_id": "123456789012",
        "readonly_role_name": "openci-tf-executor-readonly",
        "poweruser_role_name": "openci-tf-executor-poweruser",
        "external_id": "openci-tf-0123456789abcdef",
        "max_ttl": 3600,
    }
    record = IntentRecord(
        token="abc123",
        trigger_id="t",
        pr_number=1,
        action="apply",
        source_run_id="run1",
        folders=("infra/vpc",),
        commit_hash="a" * 40,
        folder_pins=(
            FolderPlanPin(
                "infra/vpc",
                "run1",
                "b" * 64,
                "plan.tfplan",
                "123456789012",
                "tofu:1.8.0",
                frozen_binding,
            ),
        ),
        expires_at=9999999999,
    )
    monkeypatch.setattr("src.services.intent.confirm.get_intent", lambda _: record)
    monkeypatch.setattr(
        "src.services.intent.confirm.mark_intent_used", lambda *_args, **_kwargs: record
    )

    failures, confirmed = confirm_intent(
        token="abc123",
        action="apply",
        commit_hash="a" * 40,
        trigger_id="t",
        pr_number=1,
        repo_name="o/r",
    )

    assert failures == []
    assert confirmed is not None
    assert confirmed["folder_pins"]["infra/vpc"]["account_id"] == "123456789012"
    assert confirmed["folder_pins"]["infra/vpc"]["account_binding"] == frozen_binding


def test_token_single_use_race(monkeypatch):
    table = Mock()
    monkeypatch.setattr("src.platform.aws.intent_registry._table", lambda: table)
    record = IntentRecord(
        token="abc123",
        trigger_id="t",
        pr_number=1,
        action="apply",
        source_run_id="run1",
        folders=("infra/vpc",),
        commit_hash="a" * 40,
        folder_pins=(
            FolderPlanPin(
                "infra/vpc",
                "run1",
                "b" * 64,
                "plan.tfplan",
                "123456789012",
                "tofu:1.8.0",
                {
                    "account_id": "123456789012",
                    "readonly_role_name": "openci-tf-executor-readonly",
                    "poweruser_role_name": "openci-tf-executor-poweruser",
                    "external_id": "openci-tf-0123456789abcdef",
                    "max_ttl": 3600,
                },
            ),
        ),
        expires_at=9999999999,
    )
    table.update_item.return_value = {
        "Attributes": {
            "token": record.token,
            "trigger_id": record.trigger_id,
            "pr_number": record.pr_number,
            "action": record.action,
            "source_run_id": record.source_run_id,
            "folders": list(record.folders),
            "commit_hash": record.commit_hash,
            "folder_pins": [
                {
                    "folder": pin.folder,
                    "source_run_id": pin.source_run_id,
                    "plan_sha256": pin.plan_sha256,
                    "plan_artifact_name": pin.plan_artifact_name,
                    "account_id": pin.account_id,
                    "tf_runtime": pin.tf_runtime,
                    "account_binding": pin.account_binding,
                }
                for pin in record.folder_pins
            ],
            "expires_at": record.expires_at,
            "used": True,
        }
    }
    confirmed = mark_intent_used("abc123", trigger_id="t", pr_number=1, now=1)
    assert confirmed.used is True
    table.update_item.assert_called_once()


def test_confirm_gate_commit_mismatch():
    record = IntentRecord(
        token="abc123",
        trigger_id="t",
        pr_number=1,
        action="apply",
        source_run_id="run1",
        folders=("infra/vpc",),
        commit_hash="a" * 40,
        folder_pins=(),
        expires_at=9999999999,
    )
    failures = evaluate_confirm_gates(
        record=record,
        commit_hash="b" * 40,
        trigger_id="t",
        pr_number=1,
        repo_name="o/r",
        now=1,
    )
    assert any("PR moved" in failure.message for failure in failures)


def test_mutation_actions_resolve():
    config = FolderConfig(
        account_alias="target",
        apply=MutationVerbConfig(allow=True),
        destroy=MutationVerbConfig(allow=True, grace_seconds=60),
    )
    assert resolve_commands("apply", config).verb == "apply"
    assert resolve_commands("destroy", config).verb == "destroy"
    assert resolve_commands("plan_destroy", config).verb == "plan_destroy"


def test_token_format():
    token = mint_token()
    assert 6 <= len(token) <= 8
    assert all(ch in "0123456789abcdef" for ch in token)
