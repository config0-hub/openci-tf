# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.domain.config.folder_config import compact_folder_config_for_outer_state, parse_folder_config
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.outer_map_state import build_compact_resolve_result, merge_map_item
from src.domain.engine.summary import build_outer_map_outcome
from src.platform.aws.run_registry.step_index import registry_step_index_from_state
from src.domain.formatters.artifacts import summary
from src.services.render import handler as render_handler
from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition


def _outcome(folder: str, *, succeeded: bool, step_index: int | None = None) -> dict[str, object]:
    return build_outer_map_outcome(
        folder=folder,
        account_id="123456789012",
        execution_id=f"exec-{folder.replace('/', '-')}",
        output={"exec_id": f"exec-{folder.replace('/', '-')}", "succeeded": succeeded},
        step_index=step_index,
    )


def _state() -> dict[str, object]:
    return {
        "step_index": 0,
        "step_count": 3,
        "steps": [["infra/vpc"], ["infra/rds", "infra/ec2"], ["infra/app"]],
        "outcomes": [],
        "skipped": [],
        "map_items": [
            {"folder": "infra/vpc", "account_id": "123456789012", "e": "exec-vpc", "step_index": 0},
            {"folder": "infra/rds", "account_id": "123456789012", "e": "exec-rds", "step_index": 1},
            {"folder": "infra/ec2", "account_id": "123456789012", "e": "exec-ec2", "step_index": 1},
            {"folder": "infra/app", "account_id": "123456789012", "e": "exec-app", "step_index": 2},
        ],
        "current_step_items": [
            {"folder": "infra/vpc", "account_id": "123456789012", "e": "exec-vpc", "step_index": 0}
        ],
    }


def test_collect_step_outcomes_advances_to_next_step_after_success() -> None:
    result = render_handler.handler(
        {
            "collect_step_outcomes": True,
            "state": _state(),
            "step_outcomes": [_outcome("infra/vpc", succeeded=True)],
        },
        object(),
    )

    assert result["step_failed"] is False
    assert result["step_index"] == 1
    assert [item["folder"] for item in result["current_step_items"]] == ["infra/rds", "infra/ec2"]
    assert result["outcomes"][0]["step_index"] == 0


def test_collect_step_outcomes_stops_after_mid_pipeline_failure_and_marks_later_steps_not_run() -> None:
    state = _state()
    state["step_index"] = 1
    state["outcomes"] = [_outcome("infra/vpc", succeeded=True, step_index=0)]

    result = render_handler.handler(
        {
            "collect_step_outcomes": True,
            "state": state,
            "step_outcomes": [
                _outcome("infra/rds", succeeded=True),
                _outcome("infra/ec2", succeeded=False),
            ],
        },
        object(),
    )

    assert result["step_failed"] is True
    assert result["step_index"] == 2
    assert [item["folder"] for item in result["skipped"]] == ["infra/app"]
    assert result["skipped"][0]["status"] == "skipped"
    assert result["skipped"][0]["reply"] == "not run"


def _full_item(folder: str, step_index: int) -> dict[str, object]:
    run_id = "r" * 32
    return {
        "run_id": run_id,
        "folder": folder,
        "account_id": "123456789012",
        "account_binding": ["role", None, "session", 3600],
        "action": "plan",
        "attempt": 0,
        "budget": 300,
        "deadline_at": "2099-01-01T00:00:00Z",
        "step_index": step_index,
        "folder_config": compact_folder_config_for_outer_state(asdict(parse_folder_config("account_alias: target\n"))),
        "upstream_urls": {"tofu:1.8.0": "https://example.invalid/tofu"},
        "execution_id": compose_execution_id(run_id, folder, 0),
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
    }


def test_twenty_folder_pipeline_split_keeps_outer_state_budget() -> None:
    step_one = [f"infra/a-{index}" for index in range(10)]
    step_two = [f"infra/b-{index}" for index in range(10)]
    items = [_full_item(folder, 0) for folder in step_one] + [_full_item(folder, 1) for folder in step_two]

    result = build_compact_resolve_result(
        {
            "run_id": "r" * 32,
            "webhook_info": {"repo_name": "org/repo", "commit_hash": "a" * 40},
            "settings": {"ssm_openci_tf_github_token": "/openci-tf/github/token"},
            "action": "plan",
            "folders": step_one + step_two,
            "steps": [step_one, step_two],
            "notification_target": {"type": "registry"},
            "pipeline": "data/primary",
        },
        run_id="r" * 32,
        full_items=items,
        skipped=[],
    )

    assert result["step_count"] == 2
    assert len(result["current_step_items"]) == 10


def test_pipeline_summary_renders_per_step_status_table() -> None:
    rendered = summary(
        [
            {"folder": "infra/vpc", "status": "succeeded", "account_id": "123456789012", "step_index": 0},
            {"folder": "infra/rds", "status": "succeeded", "account_id": "123456789012", "step_index": 1},
            {"folder": "infra/ec2", "status": "infrastructure_error", "account_id": "123456789012", "step_index": 1},
            {"folder": "infra/app", "status": "skipped", "account_id": "123456789012", "step_index": 2},
        ],
        steps=[["infra/vpc"], ["infra/rds", "infra/ec2"], ["infra/app"]],
    )

    assert "Step 1/3 · infra/vpc · ok" in rendered
    assert "Step 2/3 · infra/rds, infra/ec2 · failed" in rendered
    assert "Step 3/3 · infra/app · not run" in rendered
    assert "| `infra/app` | `123456789012` | not run | not run | n/a |" in rendered


def test_pipeline_step_index_matches_between_inner_collect_and_final_replay() -> None:
    """2-step pipeline: inner collect and RenderPR replay must agree on registry step_index."""
    step_one_folder = "terraform/primary/ap-northeast-1/01-vpc"
    step_two_folder = "terraform/primary/ap-northeast-1/03-sqs"
    steps = [[step_one_folder], [step_two_folder]]
    full_items = [_full_item(step_one_folder, 0), _full_item(step_two_folder, 1)]
    resolved = build_compact_resolve_result(
        {
            "run_id": "r" * 32,
            "webhook_info": {"repo_name": "org/repo", "commit_hash": "a" * 40},
            "settings": {"ssm_openci_tf_github_token": "/openci-tf/github/token"},
            "action": "plan",
            "folders": [step_one_folder, step_two_folder],
            "steps": steps,
            "notification_target": {"type": "github_pr"},
            "pipeline": "primary-msg",
        },
        run_id="r" * 32,
        full_items=full_items,
        skipped=[],
    )
    # Simulate a stale map item step_index on step 2 while steps remain authoritative.
    resolved["map_items"][1]["step_index"] = 0

    stored: list[int] = []
    for step_cursor in (0, 1):
        current_items = resolved["current_step_items"]
        assert [item["folder"] for item in current_items] == [steps[step_cursor][0]]
        for item in current_items:
            inner_event = merge_map_item(resolved["map_shared"], item)
            stored.append(registry_step_index_from_state(inner_event.get("step_index")))
        step_outcomes = [
            build_outer_map_outcome(
                folder=item["folder"],
                account_id="123456789012",
                execution_id=str(item["e"]),
                output={"exec_id": str(item["e"]), "succeeded": True},
                step_index=item.get("step_index") if isinstance(item.get("step_index"), int) else None,
            )
            for item in current_items
        ]
        resolved = render_handler.handler(
            {
                "collect_step_outcomes": True,
                "state": resolved,
                "step_outcomes": step_outcomes,
            },
            object(),
        )

    replayed: list[int] = []
    for outcome in resolved["outcomes"]:
        replayed.append(registry_step_index_from_state(outcome.get("step_index")))

    assert stored == [1, 2]
    assert replayed == [1, 2]
    assert stored == replayed


def test_readonly_pipeline_collect_attempts_match_render_pr_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay the two-step readonly pipeline shape captured from execution history."""
    run_id = "1787660208876.7e34ddd6"
    account_id = "998038917735"
    folders = [
        "terraform/primary/ap-northeast-1/03-sqs",
        "terraform/primary/ap-northeast-1/06-sns-topic",
    ]
    execution_ids = [
        f"{run_id}.4b4bc82fdc99.0",
        f"{run_id}.f445281ac67b.0",
    ]
    collect_parameters = load_rendered_run_folder_definition("read")["States"][
        "Collect"
    ]["Parameters"]
    collect_step_path = collect_parameters.get("step_index.$")
    attempt_items = {
        folder: {
            "run_id": run_id,
            "folder": folder,
            "account_id": account_id,
            "execution_id": execution_id,
            "attempt": 0,
            "status": "succeeded",
            "step_index": registry_step_index_from_state(
                step_index if collect_step_path == "$.step_index" else None
            ),
        }
        for step_index, (folder, execution_id) in enumerate(
            zip(folders, execution_ids, strict=True)
        )
    }

    def replay_folder_attempt(**item: object) -> None:
        existing = attempt_items[str(item["folder"])]
        if existing["step_index"] != item["step_index"]:
            raise ValueError("attempt item replay mismatch on step_index")

    from src.platform.aws import run_registry

    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(
        render_handler.boto3,
        "resource",
        lambda _service: SimpleNamespace(Table=lambda _name: object()),
    )
    monkeypatch.setattr(render_handler.run_lock, "release", lambda *_args: None)
    monkeypatch.setattr(run_registry, "put_folder_record", replay_folder_attempt)
    monkeypatch.setattr(run_registry, "update_run_status", lambda *_args, **_kwargs: None)

    result = render_handler.handler(
        {
            "run_id": run_id,
            "webhook_info": {
                "repo_name": "williaumwu/openci-test-gitops",
                "notification_target": {"type": "registry"},
            },
            "notification_target": {"type": "registry"},
            "action": "plan",
            "outcomes": [
                {
                    "folder": folder,
                    "account_id": account_id,
                    "execution_id": execution_id,
                    "output": {
                        "exec_id": execution_id,
                        "succeeded": True,
                        "attempt": 0,
                        "manifest_s3_uri": f"s3://tmp/{folder}/manifest.json",
                        "manifest_sha256": "a" * 64,
                    },
                    "step_index": step_index,
                }
                for step_index, (folder, execution_id) in enumerate(
                    zip(folders, execution_ids, strict=True)
                )
            ],
            "skipped": [],
        },
        object(),
    )

    assert result["registry_only"] is True
    assert [item["step_index"] for item in attempt_items.values()] == [1, 2]


def test_confirmed_pipeline_apply_step_index_matches_between_collect_and_render_replay() -> None:
    """Confirmed apply step 2: mutation collect and RenderPR replay agree on registry step_index."""
    step_two_folder = "terraform/primary/ap-northeast-1/03-sqs"
    confirmed_step_index = 1
    full_items = [_full_item(step_two_folder, confirmed_step_index)]
    resolved = build_compact_resolve_result(
        {
            "run_id": "r" * 32,
            "webhook_info": {
                "repo_name": "org/repo",
                "commit_hash": "a" * 40,
                "pipeline": "primary-msg",
                "pipeline_step_index": 2,
                "pipeline_step_count": 2,
            },
            "settings": {"ssm_openci_tf_github_token": "/openci-tf/github/token"},
            "action": "apply",
            "folders": [step_two_folder],
            "notification_target": {"type": "github_pr"},
        },
        run_id="r" * 32,
        full_items=full_items,
        skipped=[],
    )
    resolved["step_index"] = confirmed_step_index
    resolved["map_items"][0]["step_index"] = 0

    item = resolved["map_items"][0]
    inner_event = merge_map_item(resolved["map_shared"], item)
    inner_event["step_index"] = resolved["step_index"]
    stored = registry_step_index_from_state(inner_event.get("step_index"))

    outcome = render_handler.handler(
        {
            "normalize_folder_outcome": True,
            "state": {
                **inner_event,
                "child_execution": {
                    "Output": {
                        "exec_id": str(item["e"]),
                        "succeeded": True,
                    }
                },
            },
        },
        object(),
    )
    replayed = registry_step_index_from_state(outcome.get("step_index"))

    assert stored == 2
    assert replayed == 2
    assert stored == replayed
