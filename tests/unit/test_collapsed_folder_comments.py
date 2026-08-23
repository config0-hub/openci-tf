"""Focused tests for collapsed per-folder PR comments and account-id propagation."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.domain.formatters.artifacts import (
    bound_comment,
    folder_comment,
    pending_plan_comment,
    pending_summary,
    summary,
)
from src.services.render import handler as render_handler
from src.services.resolve import validate_and_resolve as resolve

_ACCOUNT = "123456789012"
_FULL_SHA = "a" * 40
_CLONE_TOKEN = "/openci-tf/clone-token/test"
_GITHUB_URL = "https://github.com/org/repo.git"


def _outcome(**overrides):
    base = {"folder": "infra/a", "account_id": _ACCOUNT, "status": "succeeded", "succeeded": True}
    base.update(overrides)
    return base


def _artifacts():
    return {
        "init.out": "Terraform has been successfully initialized!",
        "validate.out": "Success! The configuration is valid.",
        "tf/plan.out": "Plan: 0 to add, 0 to change, 0 to destroy",
        "tfsec.json": '{"results":[]}',
        "infracost.json": '{"totalMonthlyCost":"0"}',
    }


def _assert_collapsed_shell(body: str, *, folder: str, account_id: str, status_fragment: str) -> None:
    assert body.startswith("<details>\n<summary>")
    assert ' open' not in body.split("</summary>", 1)[0]
    assert f"`{folder}` · {account_id} ·" in body
    assert status_fragment in body
    assert body.count("<details>") == body.count("</details>")
    assert f"## Terraform: `{folder}` ({account_id})" in body or "configuration error" in body.lower()


@pytest.mark.parametrize(
    ("outcome", "status_fragment"),
    [
        (_outcome(status="in_progress", reply="Run already in progress (exec other)."), "In progress"),
        (_outcome(status="infrastructure_error", error="engine failed"), "Infrastructure error"),
        (_outcome(credential_expired=True), "Credentials expired"),
        (_outcome(succeeded=False, error="step failed"), "Failed"),
        (_outcome(), "Plan succeeded"),
    ],
)
def test_folder_comment_terminal_paths_are_collapsed_with_account_context(outcome, status_fragment):
    rendered = folder_comment("infra/a", outcome, _artifacts(), action="plan", commit_hash=_FULL_SHA)
    _assert_collapsed_shell(rendered, folder="infra/a", account_id=_ACCOUNT, status_fragment=status_fragment)


def test_folder_comment_escapes_html_in_summary():
    folder = "infra/<bad>"
    rendered = folder_comment(folder, _outcome(folder=folder), _artifacts(), commit_hash=_FULL_SHA)
    assert "<summary>`infra/&lt;bad&gt;` ·" in rendered


def test_folder_comment_requires_account_id():
    with pytest.raises(ValueError, match="missing or invalid account_id"):
        folder_comment("infra/a", {"status": "succeeded"}, _artifacts())


def test_pending_plan_comment_is_collapsed_with_account_and_hash():
    rendered = pending_plan_comment("infra/a", _ACCOUNT, _FULL_SHA, "plan")
    assert rendered.startswith("<details>")
    assert f"`infra/a` · {_ACCOUNT} · {_FULL_SHA[:7]} · Planning" in rendered
    assert f"## Terraform: `infra/a` ({_ACCOUNT})" in rendered


def test_pending_summary_includes_account_column():
    rendered = pending_summary(
        [{"folder": "infra/a", "account_id": _ACCOUNT}],
        [{"folder": "infra/b", "account_id": "210987654321", "status": "in_progress"}],
    )
    assert "| Folder | Account | Drift Check | Security | Cost |" in rendered
    assert f"| `infra/a` | `{_ACCOUNT}` | in progress |" in rendered
    assert "| `infra/b` | `210987654321` | in progress |" in rendered


def test_summary_table_includes_account_column():
    rendered = summary(
        [
            _outcome(folder="infra/good"),
            {"folder": "infra/bad", "account_id": _ACCOUNT, "status": "infrastructure_error"},
            {"folder": "config", "status": "infrastructure_error"},
        ],
        {"infra/good": _artifacts()},
    )
    assert "| Folder | Account | Drift Check | Security | Cost |" in rendered
    assert f"| `infra/good` | `{_ACCOUNT}` |" in rendered
    assert f"| `infra/bad` | `{_ACCOUNT}` | failed |" in rendered
    assert "| `config` | `—` | failed |" in rendered


def test_bound_comment_preserves_collapsed_summary_and_balanced_details():
    tag = "#openci-tf:::tag::folder-infra/a"
    body = folder_comment("infra/a", _outcome(), {
        **_artifacts(),
        "tf/plan.out": "Plan: 1 to add, 0 to change, 0 to destroy.\n" + ("+ resource aws_instance.probe\n" * 20_000),
        "infracost.json": json.dumps({"totalMonthlyCost": "5.73", "projects": []}),
    })
    rendered = bound_comment(body, max_chars=8_000, suffix=f"\n\n{tag}")
    assert len(rendered) <= 8_000
    assert rendered.count(tag) == 1
    assert rendered.startswith("<details>\n<summary>")
    assert f"`infra/a` · {_ACCOUNT} ·" in rendered
    assert rendered.count("```") % 2 == 0
    assert rendered.lower().count("<details>") == rendered.lower().count("</details>")


def test_resolve_attaches_account_id_to_map_items_and_skipped(monkeypatch):
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(resolve.boto3, "resource", lambda *_: SimpleNamespace(Table=lambda _: object()))
    monkeypatch.setattr(resolve, "get_github_token", lambda _: "token")
    monkeypatch.setattr(resolve, "shallow_clone", lambda *_args, **_kwargs: "clone")
    monkeypatch.setattr(resolve, "cleanup_clone", lambda _: None)
    monkeypatch.setattr(resolve, "validate_reserved_package_names", lambda _: None)
    monkeypatch.setattr(resolve, "load_account_alias", lambda _: SimpleNamespace(account_id=_ACCOUNT, role_name="target", poweruser_role_name=None, external_id="openci-tf-6be00970ed31c57d", max_ttl=3600))
    monkeypatch.setattr(
        resolve,
        "resolve_outer_state",
        lambda *_: {"folder_configs": {"infra/a": {"account_alias": "target"}}, "upstream_urls": {"tofu": "https://example/tofu"}},
    )
    monkeypatch.setattr(resolve.run_lock, "acquire", lambda *_: None)
    launched = resolve.handler({
        "action": "plan",
        "folders": ["infra/a"],
        "all_flag": False,
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "trigger_id": "trigger", "comment_body": "tf plan infra/a", "commit_hash": _FULL_SHA},
        "settings": {"git_url": _GITHUB_URL, "ssm_openci_tf_github_token": _CLONE_TOKEN, "upstream_urls": {"tofu": "https://example/tofu"}},
    }, None)
    assert launched["map_items"][0]["account_id"] == _ACCOUNT

    from src.core.errors import LockHeldError

    monkeypatch.setattr(resolve.run_lock, "acquire", lambda *_: (_ for _ in ()).throw(LockHeldError("run already in progress (exec other)")))
    skipped = resolve.handler({
        "action": "plan",
        "folders": ["infra/a"],
        "all_flag": False,
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "trigger_id": "trigger", "comment_body": "tf plan infra/a", "commit_hash": _FULL_SHA},
        "settings": {"git_url": _GITHUB_URL, "ssm_openci_tf_github_token": _CLONE_TOKEN, "upstream_urls": {"tofu": "https://example/tofu"}},
    }, None)
    assert skipped["skipped"][0]["account_id"] == _ACCOUNT


def test_render_placeholder_uses_account_id_from_map_items(monkeypatch):
    monkeypatch.setattr(render_handler, "get_github_token", lambda _: "token")
    monkeypatch.setattr(render_handler, "GitHubClient", lambda _: SimpleNamespace())
    comments = []
    monkeypatch.setattr(
        render_handler,
        "_delete_and_repost",
        lambda *_args, **kwargs: comments.append((_args[3], _args[5])) or 1,
    )
    render_handler.handler({
        "placeholder": True,
        "action": "plan",
        "webhook_info": {"repo_name": "org/repo", "pr_number": 7, "commit_hash": _FULL_SHA},
        "settings": {"ssm_openci_tf_github_token": _CLONE_TOKEN},
        "map_items": [{"folder": "infra/a", "account_id": _ACCOUNT}],
        "skipped": [],
    }, None)
    assert comments[0][0].startswith("<details>")
    assert f"`infra/a` · {_ACCOUNT} ·" in comments[0][0]
