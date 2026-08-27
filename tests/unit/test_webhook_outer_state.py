# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end outer-input coverage for the webhook's safe lane."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests

from src.core.models import RepoSettings
from src.domain.engine.outer_map_state import merge_map_item
from src.domain.formatters.command_audit import append_audit_row
from src.services.resolve import validate_and_resolve
from src.services.resolve.handler import handler as parse_command
from src.services.run_folder import prepare_and_submit
from src.services.webhook import handler as webhook
from tests.helpers.fake_locks_table import FakeLocksTable

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


def _wire_webhook(
    monkeypatch,
    clone_dir: str,
    *,
    pr_state: str = "open",
    fail_first_help_delete: bool = False,
    fail_first_user_delete: bool = False,
):
    started: list[dict] = []
    posted: list[tuple[str, str]] = []
    deleted: list[int] = []
    audit_bodies: dict[int, str] = {}
    failed_deletes: set[str] = set()

    def fake_start(request):
        from src.services.orchestration.start_run import build_step_function_input

        payload = build_step_function_input(request, SETTINGS, "run-id-test")
        started.append({"input": json.dumps(payload)})
        return "run-id-test", True

    def fake_get_pr(*_):
        return {
            "state": pr_state,
            "head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}},
            "base": {"repo": {"full_name": "org/repo"}},
        }

    class FakeClient:
        def __init__(self, _token):
            pass

        def create_comment(self, repo, pr, body):
            cid = 9000 + len(posted) + 1
            posted.append((body, f"{repo}#{pr}"))
            audit_bodies[cid] = body
            return cid

        def delete_comment(self, repo, comment_id):
            if (
                fail_first_help_delete
                and "help" not in failed_deletes
                and webhook._TRANSIENT_HELP_MARKER_PREFIX in audit_bodies.get(comment_id, "")
            ):
                failed_deletes.add("help")
                raise requests.RequestException("delete failed")
            if (
                fail_first_user_delete
                and "user" not in failed_deletes
                and comment_id == 42
            ):
                failed_deletes.add("user")
                raise requests.RequestException("delete failed")
            deleted.append(comment_id)
            audit_bodies.pop(comment_id, None)

        def token_login(self):
            return "openci-bot"

        def find_comment_by_tag(self, repo, pr, tag):
            for cid, body in audit_bodies.items():
                if tag in body:
                    return cid
            return None

        def find_comments_by_tag(self, repo, pr, tag):
            return [cid for cid, body in audit_bodies.items() if tag in body]

        def find_comments_by_body_substring(self, repo, pr, needle):
            return [
                (comment["id"], comment["author_login"])
                for comment in self.find_comment_details_by_body_substring(repo, pr, needle)
            ]

        def find_comment_details_by_body_substring(self, repo, pr, needle):
            return [
                {
                    "id": cid,
                    "author_login": "openci-bot",
                    "body": body,
                    "created_at": "2026-08-18T10:03:00+00:00",
                }
                for cid, body in audit_bodies.items()
                if needle in body
            ]

        def get_comment_body(self, repo, comment_id):
            return audit_bodies.get(comment_id)

        def update_comment(self, repo, comment_id, body):
            audit_bodies[comment_id] = body
            posted.append((body, f"{repo}#audit-update"))

    monkeypatch.setattr(webhook, "get_repo_settings", lambda _: SETTINGS)
    monkeypatch.setattr(webhook, "get_github_token", lambda _: "token")
    monkeypatch.setattr(webhook, "get_pull_request", fake_get_pr)
    monkeypatch.setattr(webhook, "get_collaborator_permission", lambda *_: "write")
    monkeypatch.setattr(webhook, "start_run_from_request", fake_start)
    monkeypatch.setattr(webhook, "GitHubClient", FakeClient)
    monkeypatch.setattr(webhook, "locks_table", FakeLocksTable)
    return started, posted, deleted, audit_bodies


def _mock_account_alias(monkeypatch):
    monkeypatch.setattr(
        validate_and_resolve,
        "load_account_alias",
        lambda _: SimpleNamespace(account_id="123456789012", role_name="target", poweruser_role_name=None, external_id="openci-tf-6be00970ed31c57d", max_ttl=3600),
    )


def test_webhook_starts_thin_safe_lane_and_validate_resolves_it(tmp_path, monkeypatch):
    clone_dir = _repository(tmp_path)
    started, _posted, _deleted, _audit = _wire_webhook(monkeypatch, clone_dir)
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
    started, _posted, _deleted, _audit = _wire_webhook(monkeypatch, "")
    response = webhook.handler(_event(delivery="not-a-delivery-id"), None)
    assert response["statusCode"] == 400
    assert started == []


def _pull_request_event(*, delivery: str | None = _GUID_A, action: str = "synchronize") -> dict[str, object]:
    headers = {"X-GitHub-Event": "pull_request"}
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return {
        "trigger_id": "trigger",
        "headers": headers,
        "body": json.dumps({
            "action": action,
            "pull_request": {
                "number": 7,
                "user": {"login": "alice"},
                "head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}},
                "base": {"repo": {"full_name": "org/repo"}},
            },
            "repository": {"full_name": "org/repo"},
        }),
    }


@pytest.mark.parametrize("action", ["opened", "synchronize"])
def test_pull_request_events_are_ignored_without_starting_run(monkeypatch, action):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "")
    response = webhook.handler(_pull_request_event(action=action), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["message"] == "Event ignored"
    assert body["reason"] == "pull_request_event"
    assert started == []
    assert posted == []
    assert deleted == []


def test_pull_request_parse_command_returns_noop():
    parsed = parse_command({
        "webhook_info": {"event_type": "pull_request", "comment_body": ""},
        "settings": {},
    }, None)
    assert parsed["action"] == "noop"
    assert "affected_flag" not in parsed
    assert "auto_plan" not in parsed


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


@pytest.mark.parametrize(
    "command",
    [
        "tf plan infra/vpc",
        "tf destroy infra/vpc",
        "tf destroy confirm deadbeef",
    ],
)
def test_webhook_ignores_commands_on_closed_pull_request(monkeypatch, command):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="closed")
    response = webhook.handler(_event(command), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["message"] == "Event ignored"
    assert body["reason"] == "pull_request_not_open"
    assert started == []
    assert len(posted) >= 1
    rejection = next(body for body, _ in posted if body.startswith("openci-tf ignored the command"))
    assert "deadbeef" not in rejection
    assert "closed or merged" in rejection
    assert deleted == [42]


def test_webhook_open_pull_request_still_starts_run(monkeypatch):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["message"] == "Accepted"
    assert len(started) == 1
    assert any("## openci-tf commands" in body for body, _ in posted)
    assert "| `tf plan infra/vpc` | accepted |" in next(
        body for body, _ in posted if "## openci-tf commands" in body
    )
    assert deleted == [42]


def test_webhook_unsupported_tf_command_audit_and_transient_help(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(webhook.time, "sleep", lambda seconds: slept.append(seconds))
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf banana"), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["reason"] == "invalid_command"
    assert started == []
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf banana` | not supported |" in audit_body
    help_body = next(body for body, _ in posted if "command not accepted" in body)
    assert help_body.startswith("## openci-tf: command not accepted")
    assert slept == [10]
    assert 42 in deleted
    assert any(cid != 42 for cid in deleted)


@pytest.mark.parametrize("boundary", ["\n", "\u2028"])
def test_webhook_rejects_line_boundary_command_with_collapsed_audit(monkeypatch, boundary):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event(f"tf plan{boundary}infra/a"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["reason"] == "invalid_command"
    assert started == []
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf plan infra/a` | not supported |" in audit_body
    assert 42 in deleted


def test_webhook_redelivery_cleans_previous_transient_help(monkeypatch):
    monkeypatch.setattr(webhook.time, "sleep", lambda _seconds: None)
    started, posted, deleted, audit = _wire_webhook(
        monkeypatch,
        "",
        pr_state="open",
        fail_first_help_delete=True,
    )

    first = webhook.handler(_event("tf banana"), None)
    second = webhook.handler(_event("tf banana"), None)

    assert first["statusCode"] == 502
    assert second["statusCode"] == 200
    assert started == []
    help_posts = [body for body, _ in posted if "command not accepted" in body]
    assert len(help_posts) == 2
    assert all(webhook._TRANSIENT_HELP_MARKER_PREFIX in body for body in help_posts)
    assert not any("command not accepted" in body for body in audit.values())
    assert len([comment_id for comment_id in deleted if comment_id != 42]) == 2


def test_webhook_redelivery_reuses_closed_pr_acknowledgement(monkeypatch):
    started, posted, deleted, audit = _wire_webhook(
        monkeypatch,
        "",
        pr_state="closed",
        fail_first_user_delete=True,
    )

    first = webhook.handler(_event("tf plan infra/vpc"), None)
    second = webhook.handler(_event("tf plan infra/vpc"), None)

    assert first["statusCode"] == 502
    assert second["statusCode"] == 200
    assert started == []
    ignore_posts = [body for body, _ in posted if body.startswith("openci-tf ignored")]
    assert len(ignore_posts) == 1
    assert webhook._CLOSED_PR_IGNORE_MARKER_PREFIX in ignore_posts[0]
    assert len([body for body in audit.values() if body.startswith("openci-tf ignored")]) == 1
    assert deleted == [42]


def test_webhook_rejects_tf_plan_all_with_audit_not_supported(monkeypatch):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf plan all"), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["reason"] == "invalid_command"
    assert started == []
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf plan all` | not supported |" in audit_body
    assert 42 in deleted
    assert len(deleted) == 2


def test_webhook_rejects_bare_tf_plan(monkeypatch):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf plan"), None)
    assert json.loads(response["body"])["reason"] == "invalid_command"
    assert started == []
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf plan` | not supported |" in audit_body
    assert 42 in deleted
    assert len(deleted) == 2


def test_webhook_validates_run_request_before_audit_acceptance(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(webhook.time, "sleep", lambda seconds: slept.append(seconds))
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf plan /etc"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["reason"] == "invalid_command"
    assert started == []
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf plan /etc` | not supported |" in audit_body
    assert "| `tf plan /etc` | accepted |" not in audit_body
    assert slept == [10]
    assert 42 in deleted
    assert len(deleted) == 2


@pytest.mark.parametrize("command", ["tf drift infra/vpc", "tf drift"])
def test_webhook_rejects_tf_drift(monkeypatch, command):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event(command), None)
    assert json.loads(response["body"])["reason"] == "invalid_command"
    assert started == []
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert f"| `{command}` | not supported |" in audit_body


def test_webhook_drift_pipeline_starts_run(monkeypatch):
    started, posted, _deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf drift pipeline data/primary"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["message"] == "Accepted"
    assert len(started) == 1
    outer_input = json.loads(started[0]["input"])
    assert outer_input["action"] == "drift"
    assert outer_input["pipeline"] == "data/primary"
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf drift pipeline data/primary` | accepted |" in audit_body


def test_webhook_audit_row_carries_delivery_id_and_redelivery_is_idempotent(monkeypatch):
    started, posted, _deleted, audit = _wire_webhook(monkeypatch, "", pr_state="open")
    webhook.handler(_event("tf plan infra/vpc"), None)
    webhook.handler(_event("tf plan infra/vpc"), None)
    assert len(started) == 2
    body = next(iter(audit.values()))
    assert body.count(f"<!-- d:{_GUID_A} -->") == 1
    assert body.count("| `tf plan infra/vpc` | accepted |") == 1


def test_webhook_accepted_audit_failure_returns_502_and_starts_nothing(monkeypatch):
    started, _posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")

    def failing_audit(*_args, **_kwargs):
        raise requests.RequestException("github down")

    monkeypatch.setattr(webhook, "record_command_audit", failing_audit)
    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 502
    assert json.loads(response["body"])["error"] == "Unable to record command audit"
    assert started == []
    assert deleted == []


def test_webhook_accepted_audit_invalid_version_returns_502(monkeypatch):
    started, _posted, _deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")

    class FractionalVersionTable(FakeLocksTable):
        def update_item(self, **kwargs):
            response = super().update_item(**kwargs)
            attributes = response.get("Attributes")
            if isinstance(attributes, dict) and "version" in attributes:
                attributes["version"] = Decimal("1.5")
            return response

    monkeypatch.setattr(webhook, "locks_table", FractionalVersionTable)
    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 502
    assert json.loads(response["body"])["error"] == "Unable to record command audit"
    assert started == []


def test_webhook_accepted_audit_lock_contention_returns_502(monkeypatch):
    started, _posted, _deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    table = FakeLocksTable()
    table.items[("audit-lock", "org/repo#pr-7")] = {"holder": "other", "expires_at": 10**12}
    monkeypatch.setattr(webhook, "locks_table", lambda: table)
    monkeypatch.setattr("src.platform.github.command_audit.time.sleep", lambda _s: None)
    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 502
    assert started == []


def test_webhook_unsupported_audit_failure_returns_502(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(webhook.time, "sleep", lambda seconds: slept.append(seconds))
    started, _posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")

    def failing_audit(*_args, **_kwargs):
        raise requests.RequestException("github down")

    monkeypatch.setattr(webhook, "record_command_audit", failing_audit)
    response = webhook.handler(_event("tf banana"), None)
    assert response["statusCode"] == 502
    assert json.loads(response["body"])["error"] == "Unable to acknowledge command"
    assert started == []
    assert deleted == []
    assert slept == []


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


@pytest.mark.parametrize("status", [403, 404])
def test_webhook_unreadable_pr_is_acknowledged_like_closed(monkeypatch, status):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")

    def unreadable_get_pr(*_):
        raise _http_error(status)

    monkeypatch.setattr(webhook, "get_pull_request", unreadable_get_pr)
    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["reason"] == "pull_request_not_open"
    assert started == []
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf plan infra/vpc` | not supported |" in audit_body
    assert any(body.startswith("openci-tf ignored the command") for body, _ in posted)
    assert deleted == [42]


@pytest.mark.parametrize("error", [_http_error(500), requests.ConnectionError("down")])
def test_webhook_other_pr_read_errors_return_502(monkeypatch, error):
    started, posted, _deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")

    def failing_get_pr(*_):
        raise error

    monkeypatch.setattr(webhook, "get_pull_request", failing_get_pr)
    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 502
    assert started == []
    assert posted == []


def test_webhook_report_still_starts_run(monkeypatch):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf report"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["message"] == "Accepted"
    assert len(started) == 1
    audit_body = next(body for body, _ in posted if "## openci-tf commands" in body)
    assert "| `tf report` | accepted |" in audit_body
    assert deleted == [42]


def test_webhook_malformed_marker_audit_body_returns_acknowledgement_failure(monkeypatch):
    started, _posted, _deleted, audit = _wire_webhook(monkeypatch, "", pr_state="open")
    audit[9001] = append_audit_row(
        None,
        command_text="tf plan infra/vpc",
        status="accepted",
        repo_name="org/repo",
        pr_number=7,
        delivery_id="guid-1",
    )
    audit[9001] = "\n".join(
        line for line in audit[9001].splitlines() if not line.startswith("Created:")
    )

    response = webhook.handler(_event("tf report", delivery=_GUID_B), None)

    assert response["statusCode"] == 502
    assert json.loads(response["body"])["error"] == "Unable to record command audit"
    assert started == []


def test_webhook_redacts_confirm_comment_body_in_run_metadata(monkeypatch):
    started, posted, _deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")
    response = webhook.handler(_event("tf destroy confirm deadbeef"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["message"] == "Accepted"
    assert len(started) == 1
    outer_input = json.loads(started[0]["input"])
    assert outer_input["webhook_info"]["comment_body"] == "tf destroy confirm <redacted>"
    assert "deadbeef" not in outer_input["webhook_info"]["comment_body"]
    assert outer_input["confirm_token"] == "deadbeef"
    assert any("## openci-tf commands" in body for body, _ in posted)


def test_webhook_closed_pr_rejection_failure_returns_502(monkeypatch):
    started: list[bool] = []
    deleted: list[int] = []

    def fake_start(_request):
        started.append(True)
        return "run-id-test", True

    def failing_get_pr(*_):
        return {
            "state": "closed",
            "head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}},
            "base": {"repo": {"full_name": "org/repo"}},
        }

    class FailingClient:
        def __init__(self, _token):
            pass

        def create_comment(self, *_args, **_kwargs):
            raise requests.RequestException("network down")

        def delete_comment(self, _repo, comment_id):
            deleted.append(comment_id)

        def token_login(self):
            return "openci-bot"

        def find_comment_by_tag(self, *_args, **_kwargs):
            return None

        def find_comments_by_body_substring(self, *_args, **_kwargs):
            return []

        def find_comment_details_by_body_substring(self, *_args, **_kwargs):
            return []

        def get_comment_body(self, *_args, **_kwargs):
            return None

        def update_comment(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(webhook, "get_repo_settings", lambda _: SETTINGS)
    monkeypatch.setattr(webhook, "get_github_token", lambda _: "token")
    monkeypatch.setattr(webhook, "get_pull_request", failing_get_pr)
    monkeypatch.setattr(webhook, "get_collaborator_permission", lambda *_: "write")
    monkeypatch.setattr(webhook, "start_run_from_request", fake_start)
    monkeypatch.setattr(webhook, "GitHubClient", FailingClient)
    monkeypatch.setattr(webhook, "locks_table", FakeLocksTable)

    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 502
    assert json.loads(response["body"])["error"] == "Unable to acknowledge command"
    assert started == []
    assert deleted == []


def test_webhook_missing_pr_state_treated_as_closed(monkeypatch):
    started, posted, deleted, _audit = _wire_webhook(monkeypatch, "", pr_state="open")

    def get_pr_without_state(*_):
        return {
            "head": {"sha": _FULL_SHA, "repo": {"full_name": "org/repo"}},
            "base": {"repo": {"full_name": "org/repo"}},
        }

    monkeypatch.setattr(webhook, "get_pull_request", get_pr_without_state)
    response = webhook.handler(_event("tf plan infra/vpc"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["reason"] == "pull_request_not_open"
    assert started == []
    assert deleted == [42]
