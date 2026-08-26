# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behavior tests for safe resolution, bounded artifact reads, and render cleanup."""
from io import BytesIO
from types import SimpleNamespace
from typing import cast

import pytest

from src.core.errors import ConfigResolutionError, LockHeldError
from src.domain.engine.outer_map_state import merge_map_item
from src.platform.aws import s3
from src.services.render import handler as render
from src.services.resolve import validate_and_resolve as resolve

_CLONE_TOKEN = "/openci-tf/clone-token/test"
_GITHUB_URL = "https://github.com/org/repo.git"
_FULL_SHA = "a" * 40


def _noop_render_client():
    return SimpleNamespace(find_comments_by_tag=lambda *_: [], delete_comment=lambda *_, **__: None)


def _managed_comment_slot(action: str, folder: str, *, report_all: bool = False) -> str:
    if report_all or folder == "all":
        return "summary"
    return f"folder-{folder}"


def _capture_delete_and_repost(captured: list, *, body_index: int = 3, slot_index: int = 5):
    def _record(*args, **kwargs):
        action = args[4]
        folder = args[5]
        captured.append(
            _managed_comment_slot(action, folder, report_all=kwargs.get("report_all", False))
        )
        return 1

    return _record


def _capture_delete_and_repost_body_slot(captured: list):
    def _record(*args, **kwargs):
        action = args[4]
        folder = args[5]
        captured.append(
            (args[3], _managed_comment_slot(action, folder, report_all=kwargs.get("report_all", False)))
        )
        return 1

    return _record


def _webhook_for_run_id(**overrides):
    base = {
        "repo_name": "org/repo",
        "pr_number": 7,
        "commit_hash": _FULL_SHA,
        "trigger_id": "trigger",
        "event_type": "issue_comment",
        "comment_id": 42,
    }
    base.update(overrides)
    return base


def _safe_event():
    return {
        "action": "plan", "folders": ["infra/a"], "all_flag": False,
        "webhook_info": {"repo_name": "org/repo", "trigger_id": "trigger", "pr_number": 7, "comment_body": "tf plan infra/a", "commit_hash": _FULL_SHA},
        "settings": {"git_url": _GITHUB_URL, "ssm_openci_tf_github_token": _CLONE_TOKEN, "upstream_urls": {"tofu": "https://example/tofu"}},
    }


def test_resolve_consumes_parse_preserved_settings_contract_and_builds_inner_map_item(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    gate_observations: list[dict[str, object]] = []
    monkeypatch.setattr(
        resolve,
        "put_folder_gate_observations",
        lambda **kwargs: gate_observations.append(kwargs),
    )
    monkeypatch.setattr(resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(resolve, "resolve_outer_state", lambda *_: {"folder_configs": {"infra/a": {"account_alias": "target"}}, "upstream_urls": {"tofu": "https://example/tofu"}})
    monkeypatch.setattr(resolve, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", poweruser_role_name=None, external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(resolve.run_lock, "acquire", lambda *_, **__: None)
    monkeypatch.setattr(resolve, "set_run_deadline", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(resolve, "set_run_pipeline_metadata", lambda *_args, **_kwargs: None)
    result = resolve.handler(_safe_event(), None)
    item = result["map_items"][0]
    merged = merge_map_item(result["map_shared"], item)
    assert merged["action"] == "plan"
    assert merged["folder_config"]["account_alias"] == "target"
    assert item["account_id"] == "123456789012"
    assert result["map_shared"]["upstream_urls"] == {"tofu": "https://example/tofu"}
    assert result["map_shared"]["git_url"] == _GITHUB_URL
    assert result["map_shared"]["commit_hash"] == _FULL_SHA
    assert result["map_shared"]["ssm_openci_tf_github_token"] == _CLONE_TOKEN
    assert len(gate_observations) == 1
    observation = gate_observations[0]
    assert {key: value for key, value in observation.items() if key != "observed_at"} == {
        "run_id": result["run_id"],
        "trigger_id": "trigger",
        "repo_name": "org/repo",
        "source_sha": _FULL_SHA,
        "folder_configs": {"infra/a": {"apply": False, "destroy": False}},
    }
    assert isinstance(observation["observed_at"], int)


def test_resolve_projects_folder_gate_flags_before_observation_writes(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    gate_observations: list[dict[str, object]] = []
    monkeypatch.setattr(
        resolve,
        "put_folder_gate_observations",
        lambda **kwargs: gate_observations.append(kwargs),
    )
    monkeypatch.setattr(resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(
        resolve,
        "resolve_outer_state",
        lambda *_: {
            "folder_configs": {
                "infra/a": {"account_alias": "target"},
                "infra/b": {
                    "account_alias": "target",
                    "apply": {"allow": True, "grace_seconds": 15},
                    "destroy": {"allow": True, "grace_seconds": 60},
                },
            },
            "upstream_urls": {"tofu": "https://example/tofu"},
        },
    )
    monkeypatch.setattr(
        resolve,
        "load_account_alias",
        lambda _: SimpleNamespace(
            account_id="123456789012",
            role_name="target",
            poweruser_role_name=None,
            external_id="openci-tf-6be00970ed31c57d",
            max_ttl=3600,
        ),
    )
    monkeypatch.setattr(resolve.run_lock, "acquire", lambda *_, **__: None)
    monkeypatch.setattr(resolve, "set_run_deadline", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(resolve, "set_run_pipeline_metadata", lambda *_args, **_kwargs: None)

    event = _safe_event()
    event["folders"] = ["infra/a", "infra/b"]
    resolve.handler(event, None)

    assert len(gate_observations) == 1
    folder_configs = gate_observations[0]["folder_configs"]
    assert folder_configs == {
        "infra/a": {"apply": False, "destroy": False},
        "infra/b": {"apply": True, "destroy": True},
    }
    for flags in folder_configs.values():
        assert set(flags) == {"apply", "destroy"}
        assert type(flags["apply"]) is bool
        assert type(flags["destroy"]) is bool


def test_resolve_rejects_malformed_folder_gate_flags(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("RUN_REGISTRY_TABLE_NAME", "registry")
    monkeypatch.setattr(resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(
        resolve,
        "resolve_outer_state",
        lambda *_: {
            "folder_configs": {"infra/a": {"account_alias": "target", "apply": True}},
            "upstream_urls": {"tofu": "https://example/tofu"},
        },
    )
    monkeypatch.setattr(
        resolve,
        "load_account_alias",
        lambda _: SimpleNamespace(
            account_id="123456789012",
            role_name="target",
            poweruser_role_name=None,
            external_id="openci-tf-6be00970ed31c57d",
            max_ttl=3600,
        ),
    )
    monkeypatch.setattr(resolve.run_lock, "acquire", lambda *_, **__: None)
    monkeypatch.setattr(resolve, "set_run_deadline", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(resolve, "set_run_pipeline_metadata", lambda *_args, **_kwargs: None)

    with pytest.raises(ConfigResolutionError, match="malformed folder gate flags"):
        resolve.handler(_safe_event(), None)


def test_resolve_confirmed_pipeline_apply_uses_intent_step_index(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(
        resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object())
    )
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(
        resolve,
        "resolve_outer_state",
        lambda *_args, **_kwargs: {
            "folder_configs": {"infra/rds": {"account_alias": "target"}},
            "upstream_urls": {"tofu": "https://example/tofu"},
        },
    )
    monkeypatch.setattr(resolve.run_lock, "acquire", lambda *_, **__: None)
    binding = {
        "account_id": "123456789012",
        "readonly_role_name": "openci-tf-executor-readonly",
        "poweruser_role_name": "openci-tf-executor-poweruser",
        "external_id": "openci-tf-6be00970ed31c57d",
        "max_ttl": 3600,
    }
    event = _safe_event()
    event.update(
        {
            "action": "apply",
            "folders": ["infra/rds"],
            "intent_confirmed": True,
            "folder_pins": {
                "infra/rds": {
                    "source_run_id": "plan-run",
                    "account_id": "123456789012",
                    "account_binding": binding,
                }
            },
            "source_plan_run_id": "plan-run",
            "webhook_info": _webhook_for_run_id(
                pipeline="data/primary",
                pipeline_step_index=2,
                pipeline_step_count=3,
            ),
        }
    )

    result = resolve.handler(event, None)

    assert result["map_items"][0]["step_index"] == 1
    assert result["step_index"] == 1
    assert result["steps"] == []


def test_resolve_held_lock_is_skipped_with_reply(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(resolve, "resolve_outer_state", lambda *_: {"folder_configs": {"infra/a": {"account_alias": "target"}}, "upstream_urls": {"tofu": "https://example/tofu"}})
    monkeypatch.setattr(resolve, "load_account_alias", lambda _: SimpleNamespace(account_id="123456789012", role_name="target", poweruser_role_name=None, external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(resolve.run_lock, "acquire", lambda *_: (_ for _ in ()).throw(LockHeldError("run already in progress (exec other)")))
    result = resolve.handler(_safe_event(), None)
    assert result["map_items"] == []
    assert result["skipped"] == [{"folder": "infra/a", "account_id": "123456789012", "status": "in_progress", "reply": "Run already in progress (exec other)."}]


def test_resolve_passes_through_apply_with_confirm_metadata():
    from src.services.resolve import handler as parse_handler

    event = _safe_event()
    event["action"] = "apply"
    event["intent_confirm"] = True
    event["confirm_token"] = "abc123"
    result = parse_handler.handler(event, None)
    assert result["action"] == "apply"
    assert result["confirm_token"] == "abc123"


def test_affected_selection_with_no_matches_is_successful_no_op(monkeypatch):
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(resolve, "discover_folders", lambda *_: ["infra/a"])
    monkeypatch.setattr(
        resolve,
        "_changed_files_for_pinned_pr",
        lambda *_: [{"filename": "README.md", "status": "modified"}],
    )
    event = _safe_event()
    event["folders"] = []
    event["affected_flag"] = True
    result = resolve.handler(event, None)
    assert "no_op" not in result
    assert result["map_items"] == []
    assert result["skipped"] == []
    assert "no configured Terraform folders are affected" in result["no_op_reason"]
    assert isinstance(result["deadline_at"], str)


class _StalePinnedPullRequestClient:
    def get_pr_head_sha(self, repo: str, pr_number: int) -> str:
        return "b" * 40

    def get_pr_changed_files(
        self,
        repo: str,
        pr_number: int,
        *,
        max_files: int | None = None,
    ) -> list[dict]:
        pytest.fail("stale PR head must not fetch files")


class _MovingPinnedPullRequestClient:
    def __init__(self) -> None:
        self._heads = iter([_FULL_SHA, "b" * 40])

    def get_pr_head_sha(self, repo: str, pr_number: int) -> str:
        return next(self._heads)

    def get_pr_changed_files(
        self,
        repo: str,
        pr_number: int,
        *,
        max_files: int | None = None,
    ) -> list[dict]:
        return [{"filename": "infra/a/main.tf"}]


def test_changed_file_resolution_rejects_stale_pinned_head_before_fetch():
    client = _StalePinnedPullRequestClient()
    with pytest.raises(ConfigResolutionError, match="before changed-file retrieval"):
        resolve._changed_files_for_pinned_pr(client, "org/repo", 7, _FULL_SHA)


def test_changed_file_resolution_rejects_moving_head_after_fetch():
    client = _MovingPinnedPullRequestClient()
    with pytest.raises(ConfigResolutionError, match="after changed-file retrieval"):
        resolve._changed_files_for_pinned_pr(client, "org/repo", 7, _FULL_SHA)


def test_resolve_propagates_config_resolution_error_for_state_machine_catch(monkeypatch):
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    cleaned = []
    monkeypatch.setattr(resolve, "cleanup_clone", cleaned.append)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(resolve, "resolve_outer_state", lambda *_: (_ for _ in ()).throw(ConfigResolutionError("unknown folder: infra/missing")))
    with pytest.raises(ConfigResolutionError, match="unknown folder"):
        resolve.handler(_safe_event(), None)
    assert cleaned == ["clone"]


def test_legacy_event_data_formatters_are_gone():
    from pathlib import Path
    legacy = {"base.py", "cost_report.py", "drift.py", "infracost.py", "summary.py", "tf_fmt.py", "tf_init.py", "tf_plan.py", "tf_validate.py", "tfsec.py"}
    assert not legacy.intersection(path.name for path in Path("src/domain/formatters").glob("*.py"))


class _S3:
    def list_objects_v2(self, **_): return {"Contents": [{"Key": "run/ok.txt", "Size": 2}, {"Key": "run/big.txt", "Size": 9}, {"Key": "run/bad.bin", "Size": 2}]}
    def get_object(self, Bucket, Key):
        if Key.endswith("bad.bin"):
            return {"ContentType": "image/png", "Body": BytesIO(b"xx")}
        return {"ContentType": "text/plain; charset=utf-8", "Body": BytesIO(b"ok")}


def test_list_text_prefix_allows_text_and_rejects_size_and_content_type(monkeypatch):
    monkeypatch.setattr(s3.boto3, "client", lambda *_: _S3())
    assert s3.list_text_prefix("tmp", "run/", 4, frozenset({"text/plain", "application/json"})) == {"ok.txt": "ok", "big.txt": "[artifact rejected: exceeds size limit]", "bad.bin": "[artifact rejected: unsupported content type]"}


def _render_event():
    return {
        "webhook_info": _webhook_for_run_id(),
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "outcomes": [
            {"folder": "good", "account_id": "123456789012", "execution_id": "one", "succeeded": True},
            {"folder": "bad", "account_id": "123456789012", "execution_id": "two", "status": "infrastructure_error", "error": "engine"},
        ],
    }


def test_render_final_summary_for_multi_folder_plan_and_report(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    posted: list[str] = []
    deleted: list[str] = []
    summary_bodies: list[str] = []

    def upsert(*args, **kwargs):
        posted.append(_managed_comment_slot(args[4], args[5], report_all=kwargs.get("report_all", False)))
        if args[5] == "all":
            summary_bodies.append(args[3])
        return 1

    monkeypatch.setattr(render, "_delete_and_repost", upsert)
    monkeypatch.setattr(
        render,
        "_delete_generated_comment",
        lambda _client, _repo, _pr, action, folder, **kwargs: deleted.append(
            _managed_comment_slot(action, folder, report_all=kwargs.get("report_all", False))
        ),
    )

    plan_event = _render_event()
    plan_event["action"] = "plan"
    render.handler(plan_event, None)
    report_event = _render_event()
    report_event["action"] = "report"
    render.handler(report_event, None)

    assert posted.count("summary") == 2
    assert posted[:3] == ["folder-good", "folder-bad", "summary"]
    assert posted[-3:] == ["folder-good", "folder-bad", "summary"]
    assert deleted == []
    for body in summary_bodies:
        assert "Terraform Multi-Folder Summary" in body
        assert "[`good`]" in body
        assert "[`bad`]" in body
        assert "Legend" not in body


def test_render_final_plan_single_folder_deletes_summary(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    posted: list[str] = []
    deleted: list[str] = []
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost(posted))
    monkeypatch.setattr(
        render,
        "_delete_generated_comment",
        lambda _client, _repo, _pr, action, folder, **kwargs: deleted.append(
            _managed_comment_slot(action, folder, report_all=kwargs.get("report_all", False))
        ),
    )

    event = {
        "action": "plan",
        "webhook_info": _webhook_for_run_id(),
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "outcomes": [{"folder": "infra/a", "account_id": "123456789012", "execution_id": "one", "succeeded": True}],
        "skipped": [],
    }
    render.handler(event, None)

    assert posted == ["folder-infra/a"]
    assert deleted == ["summary"]


@pytest.mark.parametrize("failing", [False, True])
def test_render_releases_each_launched_lock_on_success_and_comment_failure(monkeypatch, failing):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    calls = []
    monkeypatch.setattr(render.run_lock, "release", lambda _, __, folder, execution_id: calls.append((folder, execution_id)))
    def upsert(*args, **kwargs):
        if failing and args[5] == "good":
            raise RuntimeError("github down")
        return 1
    monkeypatch.setattr(render, "_delete_and_repost", upsert)
    if failing:
        with pytest.raises(RuntimeError, match="github"):
            render.handler(_render_event(), None)
        assert calls == [("good", "one")]
    else:
        render.handler(_render_event(), None)
        assert calls == [("good", "one"), ("bad", "two")]


def test_render_ignores_removed_disabled_action_path(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    result = render.handler(
        {
            "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "trigger_id": "trigger", "commit_hash": "a" * 40},
            "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
            "disabled_action": "apply",
            "outcomes": [],
            "action": "apply",
            "run_id": "run123",
        },
        None,
    )
    assert result["rendered"] is True


def test_render_posts_locked_reply_without_artifact_read_or_release(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: pytest.fail("locked folder must not read artifacts"))
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_: pytest.fail("locked folder must not release lock"))
    comments = []
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost_body_slot(comments))
    render.handler({"webhook_info": _webhook_for_run_id(), "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN}, "skipped": [{"folder": "busy", "account_id": "123456789012", "status": "in_progress", "reply": "Run already in progress (exec other)."}]}, None)
    assert len(comments) == 1
    assert "Run already in progress" in comments[0][0]


def test_render_uses_retry_execution_self_reported_id_for_artifact_prefix(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    stale_outer_id = "run.deadbeef.0"
    retry_exec_id = "run.deadbeef.1"
    listed_prefixes = []
    comments = []
    monkeypatch.setattr(render, "list_text_prefix", lambda _bucket, prefix, *_: listed_prefixes.append(prefix) or {"tf/plan.out": "Plan: 1 to add, 0 to change, 0 to destroy"})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost_body_slot(comments))

    from src.domain.engine.invocation_id import derive_run_id

    run_id = derive_run_id(_webhook_for_run_id())
    expected_prefix = f"openci-tf/org/repo/{run_id}/infra/vpc/"

    render.handler({
        "webhook_info": _webhook_for_run_id(),
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "outcomes": [{"folder": "infra/vpc", "account_id": "123456789012", "execution_id": stale_outer_id, "output": {"exec_id": retry_exec_id, "status": "succeeded"}}],
    }, None)

    assert listed_prefixes == [expected_prefix]
    folder_body = next(body for body, slot in comments if slot == "folder-infra/vpc")
    assert folder_body.strip()


def test_render_posts_bounded_config_error_feedback_from_normalized_outcome(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: pytest.fail("config error has no execution artifacts"))
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_: pytest.fail("config error has no launched lock"))
    comments = []
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost_body_slot(comments))
    error = "unknown folder: infra/missing" + "x" * 600
    result = render.handler({"webhook_info": _webhook_for_run_id(), "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN}, "outcomes": [{"folder": "config", "status": "infrastructure_error", "error": error}], "skipped": []}, None)
    assert result["rendered"]
    assert len(comments) == 1
    assert comments[0][0].startswith("<details>")
    assert "configuration error" in comments[0][0].lower()
    assert error[:253] in comments[0][0]
    assert error[:256] not in comments[0][0]


def test_render_no_op_posts_clear_skip_and_deletes_transient_status(monkeypatch):
    from src.domain.engine.invocation_id import derive_run_id
    from src.domain.formatters.artifacts import status_comment_marker_prefix

    webhook = _webhook_for_run_id()
    marker = status_comment_marker_prefix(derive_run_id(webhook))
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    deleted_tags: list[str] = []
    bodies: list[str] = []

    class Client:
        def find_comments_by_tag(self, _repo, _pr, tag):
            deleted_tags.append(tag)
            return [123] if tag == marker else []

        def delete_comment(self, *_args):
            return None

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(
        render,
        "_delete_and_repost",
        lambda _client, _repo, _pr, body, action, folder, **kwargs: bodies.append(body) or 1,
    )
    result = render.handler({
        "run_id": derive_run_id(webhook),
        "webhook_info": webhook,
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "action": "plan",
        "outcomes": [],
        "skipped": [],
        "no_op_reason": "no configured Terraform folders are affected by this pull request",
    }, None)
    assert result["rendered"] is True
    assert "Plan skipped" in bodies[0]
    assert "no configured Terraform folders are affected" in bodies[0]
    assert marker in deleted_tags



def test_render_early_placeholder_posts_ci_status_before_folder_work(monkeypatch):
    from src.domain.engine.invocation_id import derive_run_id
    from src.domain.formatters.artifacts import status_comment_marker_prefix

    execution_arn = "arn:aws:states:us-east-1:123456789012:execution:openci-tf:abc"
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    created: list[tuple[str, int]] = []

    class Client:
        def __init__(self, _token=None):
            pass

        def create_comment(self, repo, pr, body):
            created.append((body, pr))
            return 424242

        def find_comments_by_tag(self, *_args):
            return []

        def delete_comment(self, *_args):
            raise AssertionError("early placeholder must not delete comments")

    monkeypatch.setattr(render, "GitHubClient", Client)
    webhook = {
        "repo_name": "org/repo",
        "pr_number": 7,
        "commit_hash": _FULL_SHA,
        "trigger_id": "trigger",
        "event_type": "issue_comment",
        "comment_id": 99,
    }
    result = render.handler({
        "early_placeholder": True,
        "action": "plan",
        "all_flag": True,
        "folders": [],
        "execution_arn": execution_arn,
        "webhook_info": webhook,
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
    }, None)
    assert result["early_placeholder_rendered"] is True
    assert result["status_comment_id"] == 424242
    assert len(created) == 1
    body, pr = created[0]
    assert pr == 7
    assert "## CI Details" in body
    assert f"+ {_FULL_SHA}" in body
    assert "+ status: in_progress" in body
    assert "[ci pipeline](" in body and execution_arn in body
    run_id = derive_run_id(webhook)
    assert status_comment_marker_prefix(run_id) in body
    assert "Terraform Multi-Folder Summary" not in body


def test_render_early_placeholder_skips_without_console_url(monkeypatch):
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render, "GitHubClient", lambda _: pytest.fail("must not post without console_url"))
    result = render.handler({
        "early_placeholder": True,
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "commit_hash": _FULL_SHA, "trigger_id": "t", "event_type": "issue_comment", "comment_id": 1},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
    }, None)
    assert result["early_placeholder_rendered"] is False


def test_render_early_placeholder_does_not_post_folder_comments(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    upserts = []
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args: upserts.append(_args) or pytest.fail("early placeholder must not upsert folder comments"))
    created = []
    monkeypatch.setattr(render, "GitHubClient", lambda _: SimpleNamespace(create_comment=lambda *_a, **_k: created.append(_a) or 1))
    render.handler({
        "early_placeholder": True,
        "action": "plan",
        "all_flag": False,
        "folders": ["infra/a", "infra/b"],
        "execution_arn": "arn:aws:states:us-east-1:1:execution:openci-tf:run",
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "commit_hash": _FULL_SHA, "trigger_id": "trigger", "event_type": "issue_comment", "comment_id": 11},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
    }, None)
    assert len(created) == 1
    assert not upserts


def test_render_placeholder_delete_and_reposts_two_folders_without_lock_release_or_artifacts(monkeypatch):
    _assert_render_placeholder_for_action(monkeypatch, "plan", "Planning at")


@pytest.mark.parametrize(
    ("action", "needle"),
    [
        ("plan", "Planning at"),
        ("drift", "Drift check running at"),
        ("report", "Report running at"),
    ],
)
def test_render_placeholder_copy_for_each_safe_verb(monkeypatch, action, needle):
    _assert_render_placeholder_for_action(monkeypatch, action, needle)


def _assert_render_placeholder_for_action(monkeypatch, action: str, needle: str):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: pytest.fail("placeholder must not read artifacts"))
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_: pytest.fail("placeholder must not release locks"))
    comments = []
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost_body_slot(comments))
    result = render.handler({
        "placeholder": True,
        "action": action,
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "commit_hash": _FULL_SHA},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "map_items": [{"folder": "infra/a", "account_id": "123456789012"}, {"folder": "infra/b", "account_id": "123456789012"}],
        "skipped": [],
    }, None)
    assert result["placeholder_rendered"] is True
    suffixes = [suffix for _, suffix in comments]
    assert suffixes.count("folder-infra/a") == 1
    assert suffixes.count("folder-infra/b") == 1
    assert suffixes.count("summary") == 1
    assert all(needle in body for body, suffix in comments if suffix.startswith("folder-"))
    assert "| `infra/a` | `123456789012` | in progress |" in comments[-1][0]
    assert "Terraform Multi-Folder Summary" in comments[-1][0]


def test_render_placeholder_includes_skipped_folders_in_summary(monkeypatch):
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    comments = []
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost_body_slot(comments))
    render.handler({
        "placeholder": True,
        "action": "plan",
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "commit_hash": _FULL_SHA},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "map_items": [{"folder": "infra/a", "account_id": "123456789012"}],
        "skipped": [{"folder": "infra/b", "account_id": "210987654321", "status": "in_progress", "reply": "Run already in progress."}],
    }, None)
    summary_body = comments[-1][0]
    assert "| `infra/a` | `123456789012` | in progress |" in summary_body
    assert "| `infra/b` | `210987654321` | in progress |" in summary_body


def test_render_placeholder_still_posts_summary_when_all_folders_locked(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    comments = []
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost_body_slot(comments))
    result = render.handler({
        "placeholder": True,
        "action": "plan",
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "commit_hash": _FULL_SHA},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "map_items": [],
        "skipped": [{"folder": "infra/a", "account_id": "123456789012", "status": "in_progress", "reply": "Run already in progress."}],
    }, None)
    assert result["placeholder_rendered"] is True
    assert comments == [("## Terraform Multi-Folder Summary\n\n| Folder | Account | Plan | Security | Cost |\n|--------|---------|------------|----------|------|\n| `infra/a` | `123456789012` | in progress | in progress | in progress |", "summary")]


def test_render_placeholder_uses_same_markers_as_final_render(monkeypatch):
    from src.domain.github.comment_object_id import format_comment_object_marker

    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render, "GitHubClient", lambda _: _noop_render_client())
    captured = []
    monkeypatch.setattr(
        render,
        "_delete_and_repost",
        lambda client, repo, pr, body, action, folder, **kwargs: captured.append(
            render._managed_comment_marker(repo, pr, action, folder, report_all=kwargs.get("report_all", False))
        )
        or 1,
    )
    render.handler({
        "placeholder": True,
        "action": "plan",
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "commit_hash": _FULL_SHA},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "map_items": [{"folder": "infra/a", "account_id": "123456789012"}],
        "skipped": [],
    }, None)
    assert captured == [
        format_comment_object_marker("org/repo", 7, "plan", "infra/a"),
        format_comment_object_marker("org/repo", 7, "plan", "all"),
    ]


def test_render_final_cleanup_deletes_only_matching_run_status_after_delete_and_reposts(monkeypatch):
    """Transient status cleanup is run-scoped: prior failed runs keep their status comments."""
    from src.domain.engine.invocation_id import derive_run_id
    from src.domain.formatters.artifacts import status_comment_marker_prefix

    webhook = _webhook_for_run_id()
    run_id = derive_run_id(webhook)
    marker = status_comment_marker_prefix(run_id)
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    upsert_order: list[str] = []
    deleted: list[int] = []

    class Client:
        def find_comments_by_tag(self, repo, pr, tag):
            if tag == marker:
                return [9001]
            if tag.startswith("#openci-tf:::status_comment\tother-run"):
                return [9002]
            return []

        def delete_comment(self, repo, comment_id):
            deleted.append(comment_id)

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(render, "_delete_and_repost", _capture_delete_and_repost(upsert_order))
    render.handler({
        "webhook_info": webhook,
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "outcomes": [{"folder": "infra/a", "account_id": "123456789012", "execution_id": "run.abc.0", "succeeded": True}],
        "skipped": [],
    }, None)
    assert upsert_order == ["folder-infra/a"]
    assert deleted == [9001]


def test_render_config_error_cleanup_uses_derived_run_id(monkeypatch):
    from src.domain.engine.invocation_id import derive_run_id
    from src.domain.formatters.artifacts import status_comment_marker_prefix

    webhook = _webhook_for_run_id()
    run_id = derive_run_id(webhook)
    deleted_tags: list[str] = []
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: pytest.fail("config error has no execution artifacts"))
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_: pytest.fail("config error has no launched lock"))

    class Client:
        def find_comments_by_tag(self, repo, pr, tag):
            deleted_tags.append(tag)
            return [555] if tag == status_comment_marker_prefix(run_id) else []

        def delete_comment(self, *_args):
            return None

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args, **_kwargs: 1)
    render.handler({
        "webhook_info": webhook,
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "outcomes": [{"folder": "config", "status": "infrastructure_error", "error": "unknown folder"}],
        "skipped": [],
    }, None)
    assert status_comment_marker_prefix(run_id) in deleted_tags


def test_render_cleanup_failure_fails_loud(monkeypatch):
    webhook = _webhook_for_run_id()
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    monkeypatch.setattr(render, "_delete_and_repost", lambda *_args, **_kwargs: 1)

    class Client:
        def find_comments_by_tag(self, *_args):
            return [1]

        def delete_comment(self, *_args):
            raise RuntimeError("github delete failed")

    monkeypatch.setattr(render, "GitHubClient", lambda _: Client())
    with pytest.raises(RuntimeError, match="github delete failed"):
        render.handler({
            "webhook_info": webhook,
            "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
            "outcomes": [{"folder": "infra/a", "account_id": "123456789012", "execution_id": "run.abc.0", "succeeded": True}],
            "skipped": [],
        }, None)


def test_render_repeated_run_replaces_generated_comments_at_bottom(monkeypatch):
    """User trigger comments stay put; generated folder/summary comments are deleted and re-posted."""
    from src.domain.github.comment_object_id import format_comment_object_marker

    class Session:
        def __init__(self):
            self.store: dict[int, str] = {1: "tf plan all"}
            self.next_id = 100
            self.deleted: list[int] = []
            self.patched: list[int] = []

        def post(self, url, json=None):
            assert json is not None
            cid = self.next_id
            self.next_id += 1
            self.store[cid] = json["body"]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id": cid})

        def patch(self, url, json=None):
            assert json is not None
            cid = int(url.rsplit("/", 1)[-1])
            self.patched.append(cid)
            self.store[cid] = json["body"]
            return SimpleNamespace(raise_for_status=lambda: None)

        def delete(self, url):
            cid = int(url.rsplit("/", 1)[-1])
            self.deleted.append(cid)
            del self.store[cid]
            return SimpleNamespace(raise_for_status=lambda: None)

        def get(self, url, params=None):
            page = (params or {}).get("page", 1)
            if page > 1:
                return SimpleNamespace(raise_for_status=lambda: None, json=list)
            comments = [{"id": cid, "body": body} for cid, body in sorted(self.store.items())]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: comments)

    session = Session()
    repo, pr = "org/repo", 7
    folder_marker = format_comment_object_marker(repo, pr, "plan", "infra/a")
    summary_marker = format_comment_object_marker(repo, pr, "plan", "all")

    class Client:
        def __init__(self, _token=None):
            pass

        def find_comments_by_tag(self, repo, pr, tag):
            return [cid for cid, body in session.store.items() if tag in body]

        def delete_comment(self, repo, comment_id):
            session.delete(f"/comments/{comment_id}")

        def create_comment(self, repo, pr, body):
            return session.post("/comments", json={"body": body}).json()["id"]

        def delete_and_repost(self, repo, pr, body, tag):
            for comment_id in self.find_comments_by_tag(repo, pr, tag):
                self.delete_comment(repo, comment_id)
            return self.create_comment(repo, pr, body)

    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render, "GitHubClient", Client)

    placeholder_event = {
        "placeholder": True,
        "action": "plan",
        "webhook_info": {"repo_name": repo, "pr_number": pr, "commit_hash": _FULL_SHA},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "map_items": [{"folder": "infra/a", "account_id": "123456789012"}],
        "skipped": [],
    }
    render.handler(placeholder_event, None)
    placeholder_ids = sorted(cid for cid, body in session.store.items() if cid != 1)
    assert len(placeholder_ids) == 2
    assert session.store[1] == "tf plan all"
    assert not session.patched

    final_event = {
        "webhook_info": {"repo_name": repo, "pr_number": pr, "commit_hash": _FULL_SHA, "trigger_id": "trigger", "event_type": "issue_comment", "comment_id": 42},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "outcomes": [{"folder": "infra/a", "account_id": "123456789012", "execution_id": "run.abc.0", "succeeded": True}],
        "skipped": [],
    }
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setattr(render.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(render, "list_text_prefix", lambda *_: {})
    monkeypatch.setattr(render, "_plan_artifact_metadata", lambda *_, **__: None)
    monkeypatch.setattr(render.run_lock, "release", lambda *_, **__: None)
    render.handler(final_event, None)

    assert session.store[1] == "tf plan all"
    assert not session.patched
    assert set(session.deleted) == set(placeholder_ids)
    final_generated = [cid for cid in session.store if cid != 1]
    assert len(final_generated) == 1
    tag_counts = {tag: 0 for tag in (folder_marker, summary_marker)}
    for body in session.store.values():
        for tag in tag_counts:
            if tag in body:
                tag_counts[tag] += 1
    assert tag_counts == {folder_marker: 1, summary_marker: 0}


@pytest.mark.parametrize("action", ["apply", "destroy"])
def test_terminal_mutation_delete_and_repost_is_markerless_history(action):
    from src.domain.github.comment_object_id import format_comment_object_marker
    from src.platform.github.client import GitHubClient

    class Client:
        def __init__(self):
            self.store: dict[int, str] = {}
            self.deleted: list[int] = []
            self.next_id = 1

        def find_comments_by_tag(self, _repo, _pr, tag):
            return [cid for cid, body in self.store.items() if tag in body]

        def delete_comment(self, _repo, comment_id):
            self.deleted.append(comment_id)
            del self.store[comment_id]

        def create_comment(self, _repo, _pr, body):
            cid = self.next_id
            self.next_id += 1
            self.store[cid] = body
            return cid

    client = Client()
    typed_client = cast(GitHubClient, client)
    repo, pr, folder = "org/repo", 7, "infra/a"
    marker = format_comment_object_marker(repo, pr, action, folder)

    first_id = render._delete_and_repost(
        typed_client, repo, pr, "first", action, folder, emit_marker=False
    )
    second_id = render._delete_and_repost(
        typed_client, repo, pr, "second", action, folder, emit_marker=False
    )

    assert client.deleted == []
    assert set(client.store) == {first_id, second_id}
    assert all(marker not in body for body in client.store.values())


@pytest.mark.parametrize(
    ("action", "comment_type"),
    [
        ("plan", "plan"),
        ("drift", "drift"),
        ("report", "report"),
        ("plan_destroy", "destroy"),
    ],
)
def test_replaceable_terminal_actions_still_carry_markers_and_replace(
    action, comment_type
):
    from src.domain.github.comment_object_id import format_comment_object_marker
    from src.platform.github.client import GitHubClient

    class Client:
        def __init__(self):
            self.store: dict[int, str] = {}
            self.deleted: list[int] = []
            self.next_id = 1

        def find_comments_by_tag(self, _repo, _pr, tag):
            return [cid for cid, body in self.store.items() if tag in body]

        def delete_comment(self, _repo, comment_id):
            self.deleted.append(comment_id)
            del self.store[comment_id]

        def create_comment(self, _repo, _pr, body):
            cid = self.next_id
            self.next_id += 1
            self.store[cid] = body
            return cid

    client = Client()
    typed_client = cast(GitHubClient, client)
    repo, pr, folder = "org/repo", 7, "infra/a"
    marker = format_comment_object_marker(repo, pr, comment_type, folder)

    first_id = render._delete_and_repost(typed_client, repo, pr, "first", action, folder)
    second_id = render._delete_and_repost(typed_client, repo, pr, "second", action, folder)

    assert client.deleted == [first_id]
    assert list(client.store) == [second_id]
    assert client.store[second_id].endswith(marker)


def test_apply_placeholder_still_carries_replace_marker(monkeypatch):
    from src.domain.formatters.artifacts import status_comment_marker_prefix
    from src.domain.github.comment_object_id import format_comment_object_marker

    class Client:
        def __init__(self):
            self.bodies: list[str] = []

        def find_comments_by_tag(self, *_args):
            return []

        def delete_comment(self, *_args):
            raise AssertionError("placeholder has no existing managed comments")

        def create_comment(self, _repo, _pr, body):
            self.bodies.append(body)
            return len(self.bodies)

    client = Client()
    repo, pr, folder = "org/repo", 7, "infra/a"
    run_id = "1700000000000.deadbeef"
    marker = format_comment_object_marker(repo, pr, "apply", folder)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(render, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render, "GitHubClient", lambda _: client)

    render.handler(
        {
            "placeholder": True,
            "action": "apply",
            "run_id": run_id,
            "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:openci-tf-apply:run",
            "webhook_info": {
                "repo_name": repo,
                "pr_number": pr,
                "commit_hash": _FULL_SHA,
            },
            "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
            "map_items": [{"folder": folder, "account_id": "123456789012"}],
            "skipped": [],
        },
        None,
    )

    folder_body = next(body for body in client.bodies if "## Apply in progress" in body)
    assert marker in folder_body
    assert status_comment_marker_prefix(run_id) in folder_body


def test_pipeline_resolution_lock_failure_releases_all_acquired_locks(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(
        resolve,
        "resolve_outer_state",
        lambda *_, **__: {
            "folder_configs": {
                "infra/a": {"account_alias": "target"},
                "infra/b": {"account_alias": "target"},
            },
            "upstream_urls": {"tofu": "https://example/tofu"},
            "folders": ["infra/a", "infra/b"],
            "steps": [["infra/a"], ["infra/b"]],
        },
    )
    monkeypatch.setattr(
        resolve,
        "load_account_alias",
        lambda _: SimpleNamespace(
            account_id="123456789012",
            role_name="target",
            poweruser_role_name=None,
            external_id="openci-tf-6be00970ed31c57d",
            max_ttl=3600,
        ),
    )
    acquired: list[str] = []
    released: list[str] = []

    def _acquire(_table, _repo, folder, *_args):
        if folder == "infra/b":
            raise LockHeldError("run already in progress (exec other)")
        acquired.append(folder)

    monkeypatch.setattr(resolve.run_lock, "acquire", _acquire)
    monkeypatch.setattr(resolve.run_lock, "release_all", lambda _table, run_id: released.append(run_id) or 1)
    event = _safe_event()
    event["folders"] = []
    event["pipeline"] = "data/primary"

    with pytest.raises(ConfigResolutionError, match="locked during pipeline resolution"):
        resolve.handler(event, None)

    assert acquired == ["infra/a"]
    assert len(released) == 1


def test_non_pipeline_read_only_lock_failure_still_skips_without_release_all(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(
        resolve,
        "resolve_outer_state",
        lambda *_, **__: {
            "folder_configs": {"infra/a": {"account_alias": "target"}},
            "upstream_urls": {"tofu": "https://example/tofu"},
            "folders": ["infra/a"],
            "steps": [["infra/a"]],
        },
    )
    monkeypatch.setattr(
        resolve,
        "load_account_alias",
        lambda _: SimpleNamespace(
            account_id="123456789012",
            role_name="target",
            poweruser_role_name=None,
            external_id="openci-tf-6be00970ed31c57d",
            max_ttl=3600,
        ),
    )
    monkeypatch.setattr(
        resolve.run_lock,
        "acquire",
        lambda *_: (_ for _ in ()).throw(LockHeldError("run already in progress (exec other)")),
    )
    monkeypatch.setattr(resolve.run_lock, "release_all", lambda *_: pytest.fail("non-pipeline skip must not release"))

    result = resolve.handler(_safe_event(), None)

    assert result["steps"] == [["infra/a"]]
    assert result["skipped"][0]["folder"] == "infra/a"
