# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-folder pipeline mutation checkpoint contract tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.domain.command.grammar import ParseError, parse_command
from src.domain.config.pipeline import Pipeline, Step, checkpoint_count, flatten_pipeline_folders
from src.domain.formatters.artifacts import pipeline_mutation_aggregate_comment
from src.domain.intent.gates import evaluate_intent_gates
from src.domain.intent.models import IntentGateFailure, IntentRecord
from src.domain.intent.plan_lookup import PlanLookupResult
from src.services.intent.create import create_intent


def _intent_gate_settings():
    return SimpleNamespace(
        trigger_id="t",
        repo_name="org/repo",
        ssm_openci_tf_github_token="/token",
        git_url="https://github.com/org/repo",
        upstream_urls={},
        require_approval=False,
    )


def _parallel_pipeline() -> Pipeline:
    return Pipeline(
        name="data/primary",
        steps=(
            Step(("infra/vpc",)),
            Step(("infra/rds", "infra/ec2")),
            Step(("infra/db",)),
        ),
    )


def test_flatten_pipeline_folders_expands_parallel_groups_deterministically():
    pipeline = _parallel_pipeline()

    assert flatten_pipeline_folders(pipeline) == (
        "infra/vpc",
        "infra/rds",
        "infra/ec2",
        "infra/db",
    )
    assert checkpoint_count(pipeline) == 4
    assert flatten_pipeline_folders(pipeline, reverse=True) == (
        "infra/db",
        "infra/ec2",
        "infra/rds",
        "infra/vpc",
    )


def test_destroy_pipeline_grammar_accepts_step_cursor():
    command = parse_command("tf destroy pipeline data/primary step 2")

    assert command.action == "destroy"
    assert command.pipeline == "data/primary"
    assert command.pipeline_step == 2


def test_multi_folder_destroy_is_rejected_with_pipeline_direction():
    with pytest.raises(ParseError, match="multi-folder destroy is not supported"):
        parse_command("tf destroy infra/a,infra/b")


def test_single_folder_destroy_remains_supported():
    command = parse_command("tf destroy infra/a")

    assert command.action == "destroy"
    assert command.folders == ["infra/a"]


def _checkpoint_record(folders: list[str], *, action: str = "apply") -> IntentRecord:
    return IntentRecord(
        token="abc123",
        trigger_id="t",
        pr_number=1,
        action=action,
        source_run_id="plan-run",
        folders=tuple(folders),
        commit_hash="a" * 40,
        folder_pins=(),
        expires_at=9999999999,
    )


def test_apply_pipeline_intent_scopes_to_single_folder_checkpoint(monkeypatch):
    stored = []

    def _fake_gates(**kwargs):
        folders = list(kwargs["folders"])
        return SimpleNamespace(
            ok=True,
            failures=[],
            record=_checkpoint_record(folders),
        )

    monkeypatch.setattr(
        "src.services.intent.create.get_repo_settings",
        lambda *_args, **_kwargs: _intent_gate_settings(),
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_github_token", lambda _path: "github-token"
    )
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_parallel_pipeline(), {}, "c" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_checkpoint",
        lambda **_kwargs: {"pipeline_sha256": "c" * 64, "run_id": "prior.run"},
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", _fake_gates)
    monkeypatch.setattr(
        "src.services.intent.create.put_intent",
        lambda record: stored.append(record),
    )

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
    assert record["folders"] == ["infra/rds"]
    assert stored[0].folders == ("infra/rds",)
    assert stored[0].step_index == 2
    assert stored[0].step_count == 4


def test_later_checkpoint_rejects_plan_before_prior_mutation(monkeypatch):
    from src.core.models import FolderConfig, MutationVerbConfig

    folder = "infra/rds"
    configs = {
        folder: FolderConfig(
            account_alias="target", apply=MutationVerbConfig(allow=True)
        )
    }

    def fake_lookup(**kwargs):
        return PlanLookupResult(
            match={
                "run_id": "1787688123671.7e34ddd6",
                "folder": folder,
                "plan_sha256": "a" * 64,
                "plan_artifact_name": "plan.tfplan",
                "tf_runtime": "terraform",
            }
        )

    monkeypatch.setattr(
        "src.domain.intent.gates.load_account_alias",
        lambda alias: SimpleNamespace(
            account_id="123456789012",
            enable_apply=True,
        ),
    )
    monkeypatch.setattr("src.domain.intent.gates.find_newest_fresh_plan_run", fake_lookup)

    result = evaluate_intent_gates(
        action="apply",
        folders=[folder],
        folder_configs=configs,
        settings=SimpleNamespace(
            trigger_id="t",
            repo_name="org/repo",
            require_approval=False,
        ),
        pr_number=1,
        commit_hash="a" * 40,
        prior_checkpoint_run_id="1788002834366.faf33c46",
    )

    assert not result.ok
    assert result.failures
    assert "fresh plan" in result.failures[0].message


def test_destroy_pipeline_intent_uses_reverse_checkpoint_order(monkeypatch):
    calls: list[int] = []

    def _fake_gates(**kwargs):
        return SimpleNamespace(
            ok=True,
            failures=[],
            record=_checkpoint_record(list(kwargs["folders"]), action="destroy"),
        )

    def _fake_checkpoint(**kwargs):
        calls.append(kwargs["step_index"])
        if kwargs["step_index"] == 1:
            return None
        return {"pipeline_sha256": "d" * 64, "run_id": "prior.destroy"}

    monkeypatch.setattr(
        "src.services.intent.create.get_repo_settings",
        lambda *_args, **_kwargs: _intent_gate_settings(),
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_github_token", lambda _path: "github-token"
    )
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_parallel_pipeline(), {}, "d" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_checkpoint",
        _fake_checkpoint,
    )
    monkeypatch.setattr("src.services.intent.create.evaluate_intent_gates", _fake_gates)
    monkeypatch.setattr("src.services.intent.create.put_intent", lambda _record: None)

    failure, record = create_intent(
        action="destroy",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=1,
    )

    assert failure is None
    assert record is not None
    assert record["folders"] == ["infra/db"]
    assert calls == []


def test_aggregate_comment_is_bounded_balanced_and_redacts_tokens():
    body = pipeline_mutation_aggregate_comment(
        action="apply",
        pipeline="acceptance-cbea57b",
        commit_hash="a" * 40,
        requested_command="tf apply confirm deadbeef",
        checkpoint_index=1,
        checkpoint_count=2,
        folder="terraform/primary/ap-northeast-1/05-s3-bucket",
        account_id="998038917735",
        succeeded=True,
        plan_show_text="Plan: 1 to add, 0 to change, 0 to destroy",
        pinned_plan_artifact="plan.tfplan",
        metadata_lines=["- Confirmation commands: `tf apply confirm <redacted>`"],
    )

    assert "deadbee" not in body
    assert "<redacted>" in body
    assert body.count("<details>") == body.count("</details>")
    assert len(body.encode("utf-8")) <= 65_536
    assert "Next step" not in body or "Metadata" in body


def test_destroy_pipeline_checkpoint_requires_prior_step(monkeypatch):
    monkeypatch.setattr(
        "src.services.intent.create.get_repo_settings",
        lambda *_args, **_kwargs: _intent_gate_settings(),
    )
    monkeypatch.setattr(
        "src.services.intent.create.get_github_token", lambda _path: "github-token"
    )
    monkeypatch.setattr(
        "src.services.intent.create._pipeline_for_intent",
        lambda **_kwargs: (_parallel_pipeline(), {}, "d" * 64),
    )
    monkeypatch.setattr(
        "src.services.intent.create.find_latest_successful_pipeline_checkpoint",
        lambda **_kwargs: None,
    )

    failure, record = create_intent(
        action="destroy",
        folders=[],
        trigger_id="t",
        pr_number=1,
        commit_hash="a" * 40,
        pipeline="data/primary",
        pipeline_step=2,
    )

    assert record is None
    assert failure == IntentGateFailure(
        "pipeline data/primary step 2 requires a completed destroy of step 1 first"
    )
