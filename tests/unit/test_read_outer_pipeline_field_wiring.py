# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Producer resolve output must satisfy rendered read-outer ASL JSONPath references."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from src.domain.cmd_builder.script_generator import ScriptParams, render
from src.domain.config.outer_state import resolve_outer_state
from src.domain.engine.execution_id import compose_execution_id
from src.domain.engine.outer_map_state import compact_map_item
from src.services.resolve import handler as parse_handler
from src.services.resolve import validate_and_resolve
from src.services.run_folder.prepare_and_submit import _artifact_names, _installers
from tests.helpers.asl_reachability import render_read_outer_definition

_UPSTREAMS = {
    "tofu": "https://downloads.example/tofu",
    "tfsec": "https://downloads.example/tfsec",
    "infracost": "https://downloads.example/infracost",
}


def _folder(root: Path, path: str) -> None:
    config_dir = root / path / ".openci_tf"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("account_alias: target\n")


def _pipeline(root: Path, name: str, text: str) -> None:
    path = root / ".openci_tf" / "pipelines" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _binding() -> list[object]:
    return [
        "openci-tf-executor-readonly",
        None,
        "openci-tf-0123456789abcdef",
        3600,
    ]


def _full_map_item(
    folder: str,
    *,
    action: str = "plan",
    pipeline_plan_focus: bool = False,
    run_id: str = "r" * 32,
) -> dict:
    return {
        "run_id": run_id,
        "folder": folder,
        "account_id": "123456789012",
        "account_binding": _binding(),
        "action": action,
        "attempt": 0,
        "budget": 3600,
        "deadline_at": "2099-01-01T00:00:00Z",
        "folder_config": {"account_alias": "target"},
        "upstream_urls": _UPSTREAMS,
        "execution_id": compose_execution_id(run_id, folder, 0),
        "repo_name": "org/repo",
        "git_url": "https://github.com/org/repo.git",
        "commit_hash": "a" * 40,
        "ssm_openci_tf_github_token": "/openci-tf/github/token",
        "ssm_infracost_api_key": "/openci-tf/infracost/key",
        "step_index": 0,
        "pipeline_plan_focus": pipeline_plan_focus,
    }


def _handler_event(**overrides: object) -> dict:
    event = {
        "run_id": "r" * 32,
        "webhook_info": {
            "event_type": "api",
            "repo_name": "org/repo",
            "commit_hash": "a" * 40,
            "trigger_id": "trigger",
            "idempotency_key": "delivery1",
            "pr_number": 1,
        },
        "settings": {
            "git_url": "https://github.com/org/repo.git",
            "ssm_openci_tf_github_token": "/openci-tf/github/token",
            "ssm_infracost_api_key": "/openci-tf/infracost/key",
            "upstream_urls": _UPSTREAMS,
        },
        "action": "plan",
        "folders": ["infra/a"],
        "all_flag": False,
        "affected_flag": False,
        "notification_target": {"type": "github_pr", "pr_number": 1},
        "pipeline_mutation_plan_first": False,
        "pending_mutation_action": None,
        "pipeline_plan_focus": False,
    }
    event.update(overrides)
    return event


def _wire_validate_handler(
    monkeypatch,
    *,
    folders: list[str],
    clone_dir: str | Path = "/tmp/fake-clone",
    use_real_outer_state: bool = False,
) -> None:
    config = {"account_alias": "target"}
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(
        validate_and_resolve.boto3,
        "resource",
        lambda *_: SimpleNamespace(Table=lambda _: object()),
    )
    monkeypatch.setattr(validate_and_resolve, "get_github_token", lambda *_: "token")
    monkeypatch.setattr(
        validate_and_resolve, "validate_clone_source", lambda value, *_: value
    )
    monkeypatch.setattr(
        validate_and_resolve,
        "shallow_clone",
        lambda *_args, **_kwargs: str(clone_dir),
    )
    monkeypatch.setattr(validate_and_resolve, "cleanup_clone", lambda *_: None)
    monkeypatch.setattr(
        validate_and_resolve, "validate_reserved_package_names", lambda *_: None
    )
    monkeypatch.setattr(validate_and_resolve, "_selected_folders", lambda *_: folders)
    if use_real_outer_state:
        monkeypatch.setattr(
            validate_and_resolve,
            "resolve_outer_state",
            resolve_outer_state,
        )
    else:
        monkeypatch.setattr(
            validate_and_resolve,
            "resolve_outer_state",
            lambda *_args, **_kwargs: {
                "folder_configs": {folder: config for folder in folders},
                "upstream_urls": _UPSTREAMS,
                "folders": folders,
                "steps": [folders],
            },
        )
    monkeypatch.setattr(
        validate_and_resolve,
        "load_account_alias",
        lambda *_: SimpleNamespace(
            account_id="123456789012",
            role_name="openci-tf-executor-readonly",
            poweruser_role_name=None,
            external_id="openci-tf-0123456789abcdef",
            max_ttl=3600,
        ),
    )
    monkeypatch.setattr(validate_and_resolve.run_lock, "acquire", lambda *_: None)
    monkeypatch.setattr(validate_and_resolve.run_lock, "release", lambda *_: None)
    monkeypatch.setattr(validate_and_resolve.run_lock, "release_all", lambda *_: None)


def _resolve_top_level_parameters(parameters: dict, state: dict) -> dict:
    resolved: dict = {}
    for key, value in parameters.items():
        if not key.endswith(".$"):
            resolved[key] = value
            continue
        output_key = key.removesuffix(".$")
        if value == "$$.Execution.Id":
            resolved[output_key] = "execution-arn"
            continue
        if not isinstance(value, str) or not value.startswith("$."):
            raise ValueError(f"unsupported test JSONPath {value!r}")
        state_key = value[2:]
        if state_key not in state:
            raise KeyError(state_key)
        resolved[output_key] = state[state_key]
    return resolved


def _resolve_item_selector(item_selector: dict, *, state: dict, map_item: dict) -> dict:
    resolved: dict = {}
    for key, value in item_selector.items():
        if not key.endswith(".$"):
            resolved[key] = value
            continue
        output_key = key.removesuffix(".$")
        if value == "$.step_index":
            if "step_index" not in state:
                raise KeyError("step_index")
            resolved[output_key] = state["step_index"]
            continue
        if value.startswith("$$.Map.Item.Value."):
            item_key = value.split("$$.Map.Item.Value.", 1)[1]
            if item_key not in map_item:
                raise KeyError(item_key)
            resolved[output_key] = map_item[item_key]
            continue
        if value.startswith("$.map_shared."):
            shared_key = value.split("$.map_shared.", 1)[1]
            map_shared = state.get("map_shared")
            if not isinstance(map_shared, dict) or shared_key not in map_shared:
                raise KeyError(shared_key)
            resolved[output_key] = map_shared[shared_key]
            continue
        raise ValueError(f"unsupported test JSONPath {value!r}")
    return resolved


def _assert_read_outer_paths_resolve(state: dict, definition: dict) -> None:
    render_states = ("RenderPlaceholder", "RenderPR")
    for name in render_states:
        _resolve_top_level_parameters(definition["States"][name]["Parameters"], state)

    map_item = state["map_items"][0]
    _resolve_item_selector(
        definition["States"]["RunFolders"]["ItemSelector"],
        state=state,
        map_item=map_item,
    )
    if state.get("current_step_items"):
        _resolve_item_selector(
            definition["States"]["RunStepFolders"]["ItemSelector"],
            state=state,
            map_item=state["current_step_items"][0],
        )


@pytest.fixture
def read_outer_definition() -> dict:
    return render_read_outer_definition()


def test_regular_single_folder_resolve_emits_pipeline_fields(monkeypatch, read_outer_definition):
    folders = ["infra/a"]
    _wire_validate_handler(monkeypatch, folders=folders)
    resolved = validate_and_resolve.handler(_handler_event(folders=folders), object())

    assert resolved["pipeline_plan_focus"] is False
    assert resolved["pipeline_mutation_plan_first"] is False
    assert resolved["pending_mutation_action"] is None
    assert resolved["map_items"][0]["pipeline_plan_focus"] is False
    _assert_read_outer_paths_resolve(resolved, read_outer_definition)


def test_pipeline_preview_resolve_emits_pipeline_fields(
    monkeypatch, tmp_path: Path, read_outer_definition
):
    _folder(tmp_path, "infra/vpc")
    _folder(tmp_path, "infra/rds")
    _pipeline(
        tmp_path,
        "data/primary",
        "steps:\n  - folder: infra/vpc\n  - folder: infra/rds\n",
    )
    monkeypatch.chdir(tmp_path)
    _wire_validate_handler(monkeypatch, folders=[], clone_dir=tmp_path, use_real_outer_state=True)
    event = _handler_event(
        action="plan",
        folders=[],
        pipeline="data/primary",
    )
    resolved = validate_and_resolve.handler(event, object())

    assert resolved["pipeline_plan_focus"] is True
    assert resolved["pipeline_mutation_plan_first"] is False
    assert resolved["pending_mutation_action"] is None
    assert all(item["pipeline_plan_focus"] is True for item in resolved["map_items"])
    _assert_read_outer_paths_resolve(resolved, read_outer_definition)


@pytest.mark.parametrize(
    ("pending_action", "resolved_action"),
    [("apply", "plan"), ("destroy", "plan_destroy")],
)
def test_plan_first_mutation_resolve_emits_pipeline_fields(
    monkeypatch,
    tmp_path: Path,
    read_outer_definition,
    pending_action: str,
    resolved_action: str,
):
    _folder(tmp_path, "infra/vpc")
    _folder(tmp_path, "infra/rds")
    _pipeline(
        tmp_path,
        "data/primary",
        "steps:\n  - folder: infra/vpc\n  - folder: infra/rds\n",
    )
    monkeypatch.chdir(tmp_path)
    _wire_validate_handler(monkeypatch, folders=[], clone_dir=tmp_path, use_real_outer_state=True)
    event = _handler_event(
        action=resolved_action,
        folders=[],
        pipeline="data/primary",
        pipeline_step=1,
        pipeline_mutation_plan_first=True,
        pending_mutation_action=pending_action,
    )
    resolved = validate_and_resolve.handler(event, object())

    assert resolved["action"] == resolved_action
    assert resolved["pipeline_plan_focus"] is True
    assert resolved["pipeline_mutation_plan_first"] is True
    assert resolved["pending_mutation_action"] == pending_action
    assert resolved["map_items"][0]["pipeline_plan_focus"] is True
    _assert_read_outer_paths_resolve(resolved, read_outer_definition)


def test_compact_map_item_always_carries_pipeline_plan_focus():
    item = compact_map_item(_full_map_item("infra/a", pipeline_plan_focus=False))
    assert "pipeline_plan_focus" in item
    assert item["pipeline_plan_focus"] is False


def test_parse_command_initializes_plan_first_defaults():
    result = parse_handler.handler(
        {
            "webhook_info": {
                "repo_name": "org/repo",
                "comment_body": "tf plan infra/a",
            },
            "settings": {},
        },
        None,
    )
    assert result["pipeline_mutation_plan_first"] is False
    assert result["pending_mutation_action"] is None


def test_regular_plan_keeps_tfsec_and_infracost_while_pipeline_preview_is_plan_only():
    from src.core.models import FolderConfig

    regular_installers = _installers("plan", FolderConfig(account_alias="target"), pipeline_plan_focus=False)
    focus_installers = _installers("plan", FolderConfig(account_alias="target"), pipeline_plan_focus=True)
    assert ("tfsec", "1.28.10") in regular_installers
    assert ("infracost", "0.10.39") in regular_installers
    assert ("tfsec", "1.28.10") not in focus_installers
    assert ("infracost", "0.10.39") not in focus_installers

    regular_artifacts = _artifact_names("plan", pipeline_plan_focus=False)
    focus_artifacts = _artifact_names("plan", pipeline_plan_focus=True)
    assert "tfsec.json" in regular_artifacts
    assert "infracost.json" in regular_artifacts
    assert "tfsec.json" not in focus_artifacts
    assert "infracost.json" not in focus_artifacts

    regular_script = render(ScriptParams("plan", "lambda", pipeline_plan_focus=False))
    focus_script = render(ScriptParams("plan", "lambda", pipeline_plan_focus=True))
    assert "tfsec" in regular_script
    assert "infracost" in regular_script
    assert "tfsec" not in focus_script
    assert "infracost" not in focus_script


def test_upsert_managed_comment_recovers_only_from_missing_comment():
    from unittest.mock import Mock

    from src.services.render.comments import _upsert_managed_comment

    class Client:
        def __init__(self) -> None:
            self.created = False

        def update_comment(self, _repo, _comment_id, _body):
            response = Mock(status_code=404)
            raise requests.HTTPError(response=response)

        def create_comment(self, _repo, _pr, _body):
            self.created = True
            return 99

    client = Client()
    comment_id = _upsert_managed_comment(
        client,
        "org/repo",
        1,
        "body",
        "apply",
        "all",
        existing_comment_id=42,
    )
    assert comment_id == 99
    assert client.created is True


def test_upsert_managed_comment_reraises_transient_github_errors():
    from unittest.mock import Mock

    from src.services.render.comments import _upsert_managed_comment

    class Client:
        def update_comment(self, _repo, _comment_id, _body):
            response = Mock(status_code=500)
            raise requests.HTTPError(response=response)

    with pytest.raises(requests.HTTPError):
        _upsert_managed_comment(
            Client(),
            "org/repo",
            1,
            "body",
            "apply",
            "all",
            existing_comment_id=42,
        )
