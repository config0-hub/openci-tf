# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end outer-input coverage for the webhook's safe lane."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.core.models import RepoSettings
from src.domain.engine.outer_map_state import merge_map_item
from src.services.resolve import validate_and_resolve
from src.services.resolve.handler import handler as parse_command
from src.services.run_folder import prepare_and_submit
from src.services.webhook import handler as webhook

_FULL_SHA = "a" * 40
_GUID_A = "38355582-3487-2086-500a-1b2c3d4e5f60"
_GUID_B = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

SETTINGS = RepoSettings(
    trigger_id="trigger",
    repo_name="org/repo",
    git_url="https://github.com/org/repo.git",
    secret="secret",
    ssm_openci_tf_github_token="/openci-tf/clone-token/test",
    upstream_urls={
        "tofu": "https://downloads.example/tofu",
        "tfsec": "https://downloads.example/tfsec",
        "infracost": "https://downloads.example/infracost",
    },
)


def _event(command: str = "tf plan infra/vpc", *, delivery: str | None = _GUID_A, comment_id: int = 42) -> dict[str, object]:
    headers = {"X-GitHub-Event": "issue_comment"}
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return {
        "trigger_id": "trigger",
        "headers": headers,
        "body": json.dumps({
            "action": "created",
            "comment": {"id": comment_id, "body": command, "user": {"login": "alice"}},
            "issue": {"number": 7, "pull_request": {"url": "https://api.github.example/pr/7"}},
            "repository": {"full_name": "org/repo"},
        }),
    }


def _repository(tmp_path) -> str:
    (tmp_path / ".openci_tf").mkdir()
    (tmp_path / ".openci_tf" / "config.yaml").write_text("settings:\n  tf_runtime: tofu:1.8.0\n")
    folder = tmp_path / "infra" / "vpc" / ".openci_tf"
    folder.mkdir(parents=True)
    (folder / "config.yaml").write_text("account_alias: target\n")
    return str(tmp_path)


def _wire_webhook(monkeypatch, clone_dir: str):
    started: list[dict] = []

    def fake_start(request):
        from src.services.orchestration.start_run import build_step_function_input

        payload = build_step_function_input(request, SETTINGS, "run-id-test")
        started.append({"input": json.dumps(payload)})
        return "run-id-test", True

    monkeypatch.setattr(webhook, "get_repo_settings", lambda _: SETTINGS)
    monkeypatch.setattr(webhook, "get_github_token", lambda _: "token")
    monkeypatch.setattr(webhook, "get_pull_request", lambda *_: {"head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}}, "base": {"repo": {"full_name": "org/repo"}}})
    monkeypatch.setattr(webhook, "get_collaborator_permission", lambda *_: "write")
    monkeypatch.setattr(webhook, "start_run_from_request", fake_start)
    return started


def _mock_account_alias(monkeypatch):
    monkeypatch.setattr(
        validate_and_resolve,
        "load_account_alias",
        lambda _: SimpleNamespace(account_id="123456789012", role_name="target", poweruser_role_name=None, external_id="openci-tf-6be00970ed31c57d", max_ttl=3600),
    )


def test_webhook_starts_thin_safe_lane_and_validate_resolves_it(tmp_path, monkeypatch):
    clone_dir = _repository(tmp_path)
    started = _wire_webhook(monkeypatch, clone_dir)
    assert webhook.handler(_event(), None)["statusCode"] == 200
    outer_input = json.loads(started[0]["input"])
    assert "folder_configs" not in outer_input
    assert outer_input["settings"]["upstream_urls"] == SETTINGS.upstream_urls
    assert outer_input["webhook_info"]["delivery_id"] == _GUID_A.lower()
    assert outer_input["webhook_info"]["comment_id"] == 42

    parsed = parse_command(outer_input, None)
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(validate_and_resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(validate_and_resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(validate_and_resolve, "shallow_clone", lambda *_args, **_kwargs: clone_dir)
    monkeypatch.setattr(validate_and_resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(validate_and_resolve.run_lock, "acquire", lambda *_: None)
    _mock_account_alias(monkeypatch)
    resolved = validate_and_resolve.handler(parsed, None)
    assert resolved["map_items"][0]["folder"] == "infra/vpc"
    parsed["action"] = "apply"
    parsed["folder_pins"] = {
        "infra/vpc": {
            "source_run_id": "plan-run",
            "plan_sha256": "b" * 64,
            "plan_artifact_name": "plan.tfplan",
            "account_id": "123456789012",
            "tf_runtime": "tofu:1.8.0",
            "account_binding": {
                "account_id": "123456789012",
                "readonly_role_name": "target",
                "poweruser_role_name": "poweruser",
                "external_id": "openci-tf-6be00970ed31c57d",
                "max_ttl": 3600,
            },
        }
    }
    parsed["source_plan_run_id"] = "plan-run"
    resolved_apply = validate_and_resolve.handler(parsed, None)
    assert resolved_apply["map_items"][0]["action"] == "apply"
    assert resolved_apply["map_items"][0]["folder_pin"]["source_run_id"] == "plan-run"
    assert resolved_apply["map_items"][0]["source_plan_run_id"] == "plan-run"
    compact = resolved_apply["map_items"][0]
    merged = merge_map_item(resolved_apply["map_shared"], compact)
    assert merged["folder_pin"]["plan_sha256"] == "b" * 64


def test_webhook_does_not_import_clone_or_config_resolution():
    source = __import__("pathlib").Path("src/services/webhook/handler.py").read_text()
    assert "shallow_clone" not in source and "resolve_outer_state" not in source


def test_map_item_execution_id_matches_prepare_for_its_attempt(tmp_path, monkeypatch):
    clone_dir = _repository(tmp_path)
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(validate_and_resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(validate_and_resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(validate_and_resolve, "shallow_clone", lambda *_args, **_kwargs: clone_dir)
    monkeypatch.setattr(validate_and_resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(validate_and_resolve.run_lock, "acquire", lambda *_: None)
    _mock_account_alias(monkeypatch)
    event = {
        "action": "plan", "folders": ["infra/vpc"], "all_flag": False, "run_id": "run",
        "webhook_info": {"repo_name": "org/repo", "trigger_id": "trigger", "pr_number": 7, "comment_body": "tf plan infra/vpc", "commit_hash": _FULL_SHA},
        "settings": {
            "ssm_openci_tf_github_token": "/openci-tf/clone-token/test", "git_url": "https://github.com/org/repo.git",
            "upstream_urls": SETTINGS.upstream_urls,
        },
    }
    resolved = validate_and_resolve.handler(event, None)
    merged = merge_map_item(resolved["map_shared"], resolved["map_items"][0])
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("KMS_KEY_ARN", "kms")
    monkeypatch.setenv("ENGINE_INIT_LAMBDA_NAME", "engine")
    monkeypatch.setenv("PROJECT_NAME", "openci-tf")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(prepare_and_submit.boto3, "Session", lambda: SimpleNamespace(get_credentials=lambda: None))
    monkeypatch.setattr(
        prepare_and_submit.sts,
        "get_caller_account_id",
        lambda credentials=None: "123456789012" if credentials else "REPLACE_MAIN_ACCOUNT",
    )
    monkeypatch.setattr(prepare_and_submit.sts, "assume_role", lambda *_args, **_kwargs: {"AWS_ACCESS_KEY_ID": "target"})
    monkeypatch.setattr(
        prepare_and_submit,
        "_validated_external_id",
        lambda stored, _target: stored,
    )
    monkeypatch.setattr(prepare_and_submit.s3, "presign_get", lambda *args: f"get://{args[1]}")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_put", lambda *args, **_kwargs: f"put://{args[1]}")
    monkeypatch.setattr(prepare_and_submit.s3, "presign_create_put", lambda *args: f"create-put://{args[1]}")
    monkeypatch.setattr(prepare_and_submit, "get_github_token", lambda _: "token")
    monkeypatch.setattr(prepare_and_submit, "shallow_clone", lambda *_args, **_kwargs: clone_dir)
    monkeypatch.setattr(prepare_and_submit, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(prepare_and_submit.sops, "encrypt_file", lambda path, _: path)
    monkeypatch.setattr(prepare_and_submit, "build_package", lambda *_: str(tmp_path / "package.zip"))
    monkeypatch.setattr(prepare_and_submit.s3, "upload_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_and_submit.s3, "head_object", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(prepare_and_submit.engine, "invoke_init_job", lambda *_: None)
    assert prepare_and_submit.handler(merged, None)["exec_id"] == merged["execution_id"]


def test_webhook_rejects_bad_delivery_header(monkeypatch):
    started = _wire_webhook(monkeypatch, "")
    response = webhook.handler(_event(delivery="not-a-delivery-id"), None)
    assert response["statusCode"] == 400
    assert started == []


def _pull_request_event(*, delivery: str | None = _GUID_A, request_id: str | None = None) -> dict[str, object]:
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery} if delivery else {"X-GitHub-Event": "pull_request"}
    event = {
        "httpMethod": "POST",
        "pathParameters": {"trigger_id": "trigger"},
        "headers": headers,
        "body": json.dumps({
            "action": "synchronize",
            "pull_request": {
                "number": 7,
                "user": {"login": "alice"},
                "head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}},
                "base": {"repo": {"full_name": "org/repo"}},
            },
            "repository": {"full_name": "org/repo"},
        }),
    }
    if request_id is not None:
        event["requestContext"] = {"requestId": request_id}
    return event


def test_pull_request_auto_plan_uses_affected_selection(tmp_path, monkeypatch):
    clone_dir = _repository(tmp_path)
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(validate_and_resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(validate_and_resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(validate_and_resolve, "shallow_clone", lambda *_args, **_kwargs: clone_dir)
    monkeypatch.setattr(validate_and_resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(validate_and_resolve.run_lock, "acquire", lambda *_: None)
    monkeypatch.setattr(
        validate_and_resolve.GitHubClient,
        "__init__",
        lambda self, token: None,
    )
    monkeypatch.setattr(
        validate_and_resolve.GitHubClient,
        "get_pr_head_sha",
        lambda self, repo, pr_number: _FULL_SHA,
    )
    monkeypatch.setattr(
        validate_and_resolve.GitHubClient,
        "get_pr_changed_files",
        lambda self, repo, pr_number, *, max_files=None: [{"filename": "infra/vpc/main.tf", "status": "modified"}],
    )
    _mock_account_alias(monkeypatch)

    parsed = parse_command({
        "webhook_info": {"event_type": "pull_request", "comment_body": ""},
        "settings": {},
    }, None)
    assert parsed["affected_flag"] is True
    assert parsed["all_flag"] is False

    base = {
        "action": "plan",
        "folders": [],
        "affected_flag": True,
        "settings": {
            "ssm_openci_tf_github_token": "/openci-tf/clone-token/test",
            "git_url": "https://github.com/org/repo.git",
            "upstream_urls": SETTINGS.upstream_urls,
        },
    }
    first = validate_and_resolve.handler(
        {
            **base,
            "webhook_info": {
                "event_type": "pull_request",
                "repo_name": "org/repo",
                "trigger_id": "trigger",
                "pr_number": 7,
                "commit_hash": _FULL_SHA,
                "delivery_id": _GUID_A,
            },
        },
        None,
    )
    second = validate_and_resolve.handler(
        {
            **base,
            "webhook_info": {
                "event_type": "pull_request",
                "repo_name": "org/repo",
                "trigger_id": "trigger",
                "pr_number": 7,
                "commit_hash": _FULL_SHA,
                "delivery_id": _GUID_B,
            },
        },
        None,
    )
    assert first["run_id"] != second["run_id"]
    assert first["map_items"][0]["e"] != second["map_items"][0]["e"]
    assert first["map_items"][0]["folder"] == "infra/vpc"
    assert _GUID_A not in first["run_id"] and _GUID_B not in second["run_id"]
    retry = validate_and_resolve.handler(
        {
            **base,
            "webhook_info": {
                "event_type": "pull_request",
                "repo_name": "org/repo",
                "trigger_id": "trigger",
                "pr_number": 7,
                "commit_hash": _FULL_SHA,
                "delivery_id": _GUID_A,
            },
        },
        None,
    )
    assert retry["run_id"] == first["run_id"]


def test_two_deliveries_do_not_cross_accept_done_markers(monkeypatch):
    from datetime import datetime, timezone

    from src.services.run_folder import poll_done

    monkeypatch.setenv("DONE_BUCKET_NAME", "done")
    monkeypatch.setenv("TMP_BUCKET_NAME", "tmp")
    monkeypatch.setenv("PACKAGE_BUCKET_NAME", "packages")
    submitted_at = 1_700_000_000.0
    fresh_modified = datetime.fromtimestamp(submitted_at + 2, tz=timezone.utc)
    markers = {
        "exec-a": (
            {
                "trigger_id": "exec-a",
                "status": "succeeded",
                "steps": [
                    {
                        "step_name": "step-0",
                        "status": "succeeded",
                        "exit_code": 0,
                        "duration_seconds": 1.0,
                        "output": "",
                    }
                ],
            },
            {"version_id": "v-a", "last_modified": fresh_modified},
        ),
        "exec-b": (
            {
                "trigger_id": "exec-b",
                "status": "succeeded",
                "steps": [
                    {
                        "step_name": "step-0",
                        "status": "succeeded",
                        "exit_code": 0,
                        "duration_seconds": 1.0,
                        "output": "",
                    }
                ],
            },
            {"version_id": "v-b", "last_modified": fresh_modified},
        ),
    }

    def fake_get(*_args, **_kwargs):
        exec_id = _args[1].rsplit("/", 1)[0]
        return markers[exec_id]

    monkeypatch.setattr(poll_done, "get_bounded_json_with_meta", fake_get)
    monkeypatch.setattr(poll_done.time, "sleep", lambda _: None)
    result_a = poll_done.handler(
        {"exec_id": "exec-a", "budget": 1, "deadline_at": "2099-01-01T00:00:00Z", "attempt": 0, "submitted_at": submitted_at, "done_baseline_version_id": None},
        object(),
    )
    result_b = poll_done.handler(
        {"exec_id": "exec-b", "budget": 1, "deadline_at": "2099-01-01T00:00:00Z", "attempt": 0, "submitted_at": submitted_at, "done_baseline_version_id": None},
        object(),
    )
    assert result_a["succeeded"] and result_b["succeeded"]
    assert result_a["exec_id"] != result_b["exec_id"]


def test_webhook_run_request_maps_pipeline_to_step_function_input():
    from src.services.orchestration.start_run import build_step_function_input
    from src.services.webhook.run_request import github_run_request

    request = github_run_request(
        {
            "trigger_id": "trigger",
            "commit_hash": _FULL_SHA,
            "pr_number": 7,
            "comment_id": 42,
            "event_type": "issue_comment",
            "username": "alice",
        },
        action="plan",
        folders=[],
        all_flag=False,
        affected_flag=False,
        delivery_id=_GUID_A,
        pipeline="data/primary",
    )

    payload = build_step_function_input(request, SETTINGS, "run-id-test")

    assert request.folder_mode == "pipeline"
    assert request.pipeline == "data/primary"
    assert payload["pipeline"] == "data/primary"
    assert payload["folders"] == []
    assert payload["all_flag"] is False
    assert payload["affected_flag"] is False
