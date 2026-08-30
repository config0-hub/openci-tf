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
        lambda **_kwargs: {
            "pipeline_sha256": "c" * 64,
            "run_id": "prior.run",
            "pipeline_checkpoint_completed_at": 1_700_000_000,
        },
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
                "created_at": 1_600_000_000,
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
        prior_checkpoint_completed_at=1_700_000_000,
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
        return {"pipeline_sha256": "d" * 64, "run_id": "prior.destroy", "pipeline_checkpoint_completed_at": 1}

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
        checkpoint_count=2,
        checkpoint_rows=[
            {
                "checkpoint_index": 1,
                "folder": "terraform/primary/ap-northeast-1/05-s3-bucket",
                "account_id": "998038917735",
                "plan_show_text": "Plan: 1 to add, 0 to change, 0 to destroy",
                "pinned_plan_artifact": "plan.tfplan",
                "replanned_after_prior": False,
                "confirmation_status": "Confirmed ✅",
                "result_label": "Apply succeeded ✅",
                "succeeded": True,
            }
        ],
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


def test_plan_first_mutation_resolver_uses_pipeline_plan_focus():
    from pathlib import Path

    source = Path("src/services/resolve/validate_and_resolve.py").read_text()
    plan_first_block = source.split("def _resolve_pipeline_mutation_plan_first", 1)[1].split(
        "def _project_folder_gate_flags", 1
    )[0]
    assert "pipeline_plan_focus=True" in plan_first_block
    assert 'pipeline_mutation_plan_first") is not True' not in source


def test_parse_command_routes_pipeline_apply_to_plan_first():
    from src.services.resolve import handler as parse_handler

    result = parse_handler.handler(
        {
            "webhook_info": {
                "repo_name": "org/repo",
                "comment_body": "tf apply pipeline data/primary step 2",
            },
            "settings": {},
        },
        None,
    )

    assert result["action"] == "plan"
    assert result["pipeline_mutation_plan_first"] is True
    assert result["pending_mutation_action"] == "apply"
    assert result["intent_create"] is True
    assert result["pipeline"] == "data/primary"
    assert result["pipeline_step"] == 2


def test_checkpoint_gsi_pk_scopes_pr_sha_and_pipeline_hash():
    from src.platform.aws.run_registry.keys import pipeline_checkpoint_gsi_pk

    base = dict(
        trigger_id="t",
        repo_name="org/repo",
        pipeline="data/primary",
        action="apply",
        step_index=1,
        pr_number=1,
        commit_hash="a" * 40,
        pipeline_sha256="c" * 64,
    )
    same = pipeline_checkpoint_gsi_pk(**base)
    assert pipeline_checkpoint_gsi_pk(**base) == same
    assert pipeline_checkpoint_gsi_pk(**{**base, "pr_number": 2}) != same
    assert pipeline_checkpoint_gsi_pk(**{**base, "commit_hash": "b" * 40}) != same
    assert pipeline_checkpoint_gsi_pk(**{**base, "pipeline_sha256": "d" * 64}) != same


def test_aggregate_comment_uses_total_checkpoint_count_and_cumulative_results():
    body = pipeline_mutation_aggregate_comment(
        action="apply",
        pipeline="data/primary",
        checkpoint_count=3,
        checkpoint_rows=[
            {
                "checkpoint_index": 1,
                "folder": "infra/vpc",
                "account_id": "123",
                "plan_show_text": "plan 1",
                "pinned_plan_artifact": "plan.tfplan",
                "confirmation_status": "Confirmed ✅",
                "result_label": "Apply succeeded ✅",
                "succeeded": True,
            },
            {
                "checkpoint_index": 2,
                "folder": "infra/rds",
                "account_id": "123",
                "plan_show_text": "plan 2",
                "pinned_plan_artifact": "plan.tfplan",
                "confirmation_status": "Confirmation required",
                "result_label": "Plan ready ⏳",
            },
        ],
    )

    assert "**3 checkpoints** · **1 succeeded** · **0 failed** · **2 pending**" in body
    assert "| 1/3 | `infra/vpc` |" in body
    assert "| 2/3 | `infra/rds` |" in body


def test_aggregate_comment_uses_persisted_cumulative_counts_when_rows_are_truncated():
    body = pipeline_mutation_aggregate_comment(
        action="apply",
        pipeline="data/primary",
        checkpoint_count=30,
        checkpoint_rows=[
            {
                "checkpoint_index": 30,
                "folder": "infra/f29",
                "account_id": "123",
                "plan_show_text": "plan 30",
                "pinned_plan_artifact": "plan.tfplan",
                "confirmation_status": "Confirmed ✅",
                "result_label": "Apply succeeded ✅",
                "succeeded": True,
            }
        ],
        cumulative_succeeded=25,
        cumulative_failed=3,
    )

    assert "**30 checkpoints** · **25 succeeded** · **3 failed** · **2 pending**" in body


def _pipeline_mutation_render_event(
    *,
    step_index: int,
    step_count: int,
    plan_pending: bool,
    folder: str = "infra/vpc",
) -> dict:
    return {
        "run_id": "1787000000000.abc12345",
        "pending_mutation_action": "apply",
        "pipeline_mutation_plan_first": plan_pending,
        "webhook_info": {
            "repo_name": "org/repo",
            "pr_number": 22,
            "commit_hash": "a" * 40,
            "trigger_id": "trigger",
            "pipeline": "data/primary",
            "pipeline_sha256": "c" * 64,
            "pipeline_step_index": step_index,
            "pipeline_step_count": step_count,
        },
        "settings": {"ssm_openci_tf_github_token": "/token"},
        "outcomes": [
            {
                "folder": folder,
                "account_id": "123456789012",
                "execution_id": "inner.apply.0",
                "status": "succeeded",
                "succeeded": True,
            }
        ],
        "skipped": [],
    }


def _stub_pipeline_mutation_render(monkeypatch):
    from types import SimpleNamespace

    from src.services.render import handler as render_handler

    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(render_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(
        render_handler.boto3,
        "resource",
        lambda *_: SimpleNamespace(Table=lambda _: object()),
    )
    monkeypatch.setattr(
        render_handler,
        "list_text_prefix",
        lambda *_args, **_kw: {
            "plan-show.out": "Plan: 1 to add, 0 to change, 0 to destroy",
            "apply.out": "Apply complete!",
        },
    )
    monkeypatch.setattr(render_handler, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render_handler.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render_handler, "_delete_generated_comment", lambda *_, **__: None)
    monkeypatch.setattr(render_handler, "_delete_transient_status_comment", lambda *_args: [])
    monkeypatch.setattr(render_handler, "_update_run_registry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        render_handler,
        "delete_acknowledged_command_comment",
        lambda *_, **__: [],
    )


def test_pipeline_mutation_aggregate_handler_counts_replace_plan_first_row(monkeypatch):
    from src.services.render.handler import (
        _pipeline_aggregate_identity,
        _pipeline_mutation_aggregate_body,
    )

    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    aggregate_state: dict[str, object] = {}

    def fake_get(**_kwargs):
        return aggregate_state or None

    def fake_save(**kwargs):
        aggregate_state.clear()
        aggregate_state.update(
            {
                "checkpoint_rows": kwargs["checkpoint_rows"],
                "comment_id": kwargs["comment_id"],
                "cumulative_succeeded": sum(
                    1
                    for row in kwargs["checkpoint_rows"]
                    if row.get("succeeded") is True
                ),
                "cumulative_failed": sum(
                    1
                    for row in kwargs["checkpoint_rows"]
                    if row.get("succeeded") is False
                ),
            }
        )

    monkeypatch.setattr(
        "src.platform.aws.run_registry.pipeline_aggregate.get_pipeline_aggregate_state",
        fake_get,
    )
    monkeypatch.setattr(
        "src.platform.aws.run_registry.pipeline_aggregate.save_pipeline_aggregate_state",
        fake_save,
    )

    event = _pipeline_mutation_render_event(
        step_index=1, step_count=2, plan_pending=True, folder="infra/vpc"
    )
    common = {
        "artifacts_by_folder": {
            "infra/vpc": {"plan-show.out": "Plan: 1 to add, 0 to change, 0 to destroy"}
        },
        "commit_hash": "a" * 40,
        "footer": None,
    }
    outcome = event["outcomes"][0]
    identity = _pipeline_aggregate_identity(event, "plan")
    assert identity is not None

    body, rows, _ = _pipeline_mutation_aggregate_body(
        event,
        action="plan",
        outcomes=[outcome],
        plan_pending=True,
        **common,
    )
    assert "**2 checkpoints** · **0 succeeded** · **0 failed** · **2 pending**" in body
    fake_save(comment_id=9001, checkpoint_rows=rows, **identity)

    body, rows, _ = _pipeline_mutation_aggregate_body(
        event,
        action="apply",
        outcomes=[outcome],
        plan_pending=False,
        **common,
    )
    assert "**2 checkpoints** · **1 succeeded** · **0 failed** · **1 pending**" in body
    fake_save(comment_id=9001, checkpoint_rows=rows, **identity)

    event_step2 = _pipeline_mutation_render_event(
        step_index=2, step_count=2, plan_pending=True, folder="infra/rds"
    )
    outcome_step2 = event_step2["outcomes"][0]
    common_step2 = {
        "artifacts_by_folder": {
            "infra/rds": {"plan-show.out": "Plan: 1 to add, 0 to change, 0 to destroy"}
        },
        "commit_hash": "a" * 40,
        "footer": None,
    }
    identity_step2 = _pipeline_aggregate_identity(event_step2, "plan")
    assert identity_step2 is not None

    body, rows, _ = _pipeline_mutation_aggregate_body(
        event_step2,
        action="plan",
        outcomes=[outcome_step2],
        plan_pending=True,
        **common_step2,
    )
    assert "**2 checkpoints** · **1 succeeded** · **0 failed** · **1 pending**" in body
    fake_save(comment_id=9001, checkpoint_rows=rows, **identity_step2)

    body, rows, _ = _pipeline_mutation_aggregate_body(
        event_step2,
        action="apply",
        outcomes=[outcome_step2],
        plan_pending=False,
        **common_step2,
    )
    assert "**2 checkpoints** · **2 succeeded** · **0 failed**" in body
    assert "pending" not in body.split("**2 succeeded** · **0 failed**", 1)[1].split("\n", 1)[0]


def test_pipeline_mutation_render_deletes_plan_preview_placeholder(monkeypatch):
    from src.services.render import handler as render_handler

    _stub_pipeline_mutation_render(monkeypatch)
    deleted_markers: list[str] = []

    def capture_delete(_client, _repo, _pr, marker, **kwargs):
        deleted_markers.append(marker)

    monkeypatch.setattr(render_handler, "_delete_managed_comment", capture_delete)
    monkeypatch.setattr(
        render_handler,
        "_upsert_managed_comment",
        lambda *_args, **_kwargs: 9001,
    )
    monkeypatch.setattr(
        "src.platform.aws.run_registry.pipeline_aggregate.get_pipeline_aggregate_state",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.platform.aws.run_registry.pipeline_aggregate.save_pipeline_aggregate_state",
        lambda **_kwargs: None,
    )

    render_handler.handler(
        _pipeline_mutation_render_event(step_index=1, step_count=2, plan_pending=True),
        None,
    )

    assert deleted_markers
    assert deleted_markers[0].endswith("::plan:all")


def test_pipeline_mutation_aggregate_retry_after_state_save_failure_reuses_marker(
    monkeypatch,
):
    from src.platform.aws.run_registry import RunRegistryError
    from src.services.render import handler as render_handler
    from src.services.render.comments import _managed_comment_marker

    _stub_pipeline_mutation_render(monkeypatch)
    save_attempts = 0
    aggregate_state: dict[str, object] = {}
    comments_by_marker: dict[str, int] = {}
    upsert_calls = 0

    def fake_upsert(_client, repo, pr, _body, action, folder, **kwargs):
        nonlocal upsert_calls
        upsert_calls += 1
        assert kwargs.get("emit_marker") is True
        marker = _managed_comment_marker(
            repo, pr, action, folder, report_all=kwargs.get("report_all", False)
        )
        existing = kwargs.get("existing_comment_id")
        if isinstance(existing, int) and existing > 0:
            comments_by_marker[marker] = existing
            return existing
        if marker in comments_by_marker:
            return comments_by_marker[marker]
        comment_id = 9001
        comments_by_marker[marker] = comment_id
        return comment_id

    monkeypatch.setattr(render_handler, "_upsert_managed_comment", fake_upsert)
    monkeypatch.setattr(render_handler, "_delete_pipeline_plan_preview_placeholder", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "src.platform.aws.run_registry.pipeline_aggregate.get_pipeline_aggregate_state",
        lambda **_kwargs: aggregate_state or None,
    )

    def flaky_save(**kwargs):
        nonlocal save_attempts
        save_attempts += 1
        aggregate_state.clear()
        aggregate_state.update(
            {
                "comment_id": kwargs["comment_id"],
                "checkpoint_rows": kwargs["checkpoint_rows"],
                "cumulative_succeeded": 0,
                "cumulative_failed": 0,
            }
        )
        if save_attempts == 1:
            aggregate_state.clear()
            raise RunRegistryError("failed to persist pipeline aggregate state")

    monkeypatch.setattr(
        "src.platform.aws.run_registry.pipeline_aggregate.save_pipeline_aggregate_state",
        flaky_save,
    )

    event = _pipeline_mutation_render_event(step_index=1, step_count=2, plan_pending=True)
    with pytest.raises(RunRegistryError):
        render_handler.handler(event, None)
    assert upsert_calls == 1
    assert len(comments_by_marker) == 1

    render_handler.handler(event, None)
    assert upsert_calls == 2
    assert len(comments_by_marker) == 1
    assert save_attempts == 2
