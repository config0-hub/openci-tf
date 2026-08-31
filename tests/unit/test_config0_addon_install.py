# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts for the config0-addon install path (installers, justfile, infra, release)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG0_ADDON = _REPO_ROOT / "install" / "config0_addon.py"
_REGISTER_REPO = _REPO_ROOT / "install" / "register_repo.py"
_JUSTFILE = _REPO_ROOT / "justfile"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _help_output(script: Path) -> str:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_REPO_ROOT)
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_config0_addon_help_documents_both_stages():
    help_text = _help_output(_CONFIG0_ADDON)
    assert "--stage" in help_text
    assert "ecr" in help_text
    assert "deploy" in help_text
    assert "--state-bucket" in help_text
    assert "--engine-name" in help_text


def test_register_repo_help_documents_registration_flags():
    help_text = _help_output(_REGISTER_REPO)
    for flag in ("--repo", "--trigger-id", "--webhook-url", "--upstream-urls-json"):
        assert flag in help_text


def test_api_caller_policy_entry_grants_exactly_plan_drift_report(monkeypatch):
    module = _load_module(_CONFIG0_ADDON, "config0_addon_under_test")
    role_arn = "arn:aws:iam::111122223333:role/config0-executor-remote"
    policy = module.build_api_caller_policy([role_arn], "sample-trigger")
    assert set(policy) == {role_arn}
    entry = policy[role_arn]
    assert entry["actions"] == ["plan", "drift", "report"]
    assert entry["trigger_ids"] == ["sample-trigger"]
    assert entry["binary_plan"] is False

    # The entry must round-trip through the runtime authorizer.
    from src.domain.run.api_authorization import ApiAuthorizationError, authorize_create_run

    monkeypatch.setenv("API_CALLER_POLICY_JSON", json.dumps(policy))
    event = {"requestContext": {"authorizer": {"iam": {"userArn": role_arn}}}}
    for action in ("plan", "drift", "report"):
        resolved = authorize_create_run(event, trigger_id="sample-trigger", action=action)
        assert action in resolved.actions
    with pytest.raises(ApiAuthorizationError):
        authorize_create_run(event, trigger_id="sample-trigger", action="apply")


def test_api_caller_policy_entry_rejects_non_role_arn():
    module = _load_module(_CONFIG0_ADDON, "config0_addon_arn_check")
    with pytest.raises(module.InstallError):
        module.build_api_caller_policy(["not-an-arn"], "trigger")
    with pytest.raises(module.InstallError):
        module.build_api_caller_policy(
            ["arn:aws:iam::111122223333:role/config0-executor-remote"], ""
        )


def test_config0_addon_never_touches_a_dynamodb_lock_table():
    text = _CONFIG0_ADDON.read_text(encoding="utf-8")
    assert 'client("dynamodb"' not in text
    assert "dynamodb_table" not in text
    assert "tf-locks" not in text
    assert "use_lockfile=true" in text
    # generate_backend.sh is called without the optional fifth lock-table argument.
    assert '"./scripts/generate_backend.sh", args.state_bucket, state_key, args.region, root]' in text


def _addon_recipe() -> str:
    text = _JUSTFILE.read_text(encoding="utf-8")
    return text.split("install-config0-addon:", 1)[1].split("\nuninstall:", 1)[0]


def test_install_config0_addon_recipe_composes_in_order():
    recipe = _addon_recipe()
    ecr = recipe.find("phase_timing_run addon-ecr")
    copy = recipe.find("phase_timing_run addon-image-copy")
    deploy = recipe.find("phase_timing_run addon-deploy")
    register = recipe.find("phase_timing_run addon-register")
    assert -1 not in (ecr, copy, deploy, register)
    assert ecr < copy < deploy < register
    assert "--stage ecr" in recipe
    assert "--stage deploy" in recipe
    assert "copy_ghcr_image.sh" in recipe
    assert "register_repo.py" in recipe
    assert "tf-locks" not in recipe


def test_standalone_install_recipe_is_unchanged_default():
    text = _JUSTFILE.read_text(encoding="utf-8")
    standalone = text.split("install-standalone:", 1)[1].split("install-config0-addon:", 1)[0]
    for phase in ("bootstrap", "foundation", "engine", "deploy"):
        assert f"phase_timing_run {phase} just {phase}" in standalone
    install = text.split("install *ARGS:", 1)[1].split("install-standalone:", 1)[0]
    assert 'MODE="standalone"' in install
    assert "config0-addon" in install


def test_deploy_root_supports_config0_addon_inputs():
    variables = (_REPO_ROOT / "infra/deploy/variables.tf").read_text(encoding="utf-8")
    data = (_REPO_ROOT / "infra/deploy/data.tf").read_text(encoding="utf-8")
    main = (_REPO_ROOT / "infra/deploy/main.tf").read_text(encoding="utf-8")
    for variable in ("install_mode", "state_bucket_name", "engine_name"):
        assert f'variable "{variable}"' in variables
    assert "count = local.use_lock_table ? 1 : 0" in data
    assert 'local.use_lock_table ? data.aws_dynamodb_table.locks[0].arn : ""' in main
    assert 'target_account_wildcard = var.install_mode == "config0-addon"' in main
    assert "${local.engine_name}-codebuild" in main
    assert "${local.engine_name}-worker" in main


def test_hub_setup_pattern_trust_matches_executor_roles():
    hub_main = (_REPO_ROOT / "infra/modules/hub-setup/main.tf").read_text(encoding="utf-8")
    assert "arn:aws:iam::*:role/${var.role_prefix}-executor-*" in hub_main
    assert "var.target_account_wildcard" in hub_main


def test_release_workflow_publishes_ghcr_image_with_digest():
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "ghcr.io" in workflow
    assert "scripts/image_tag.sh" in workflow
    assert "RepoDigests" in workflow
    assert "gh release" in workflow


class _FakeGitHub:
    """Records calls; serves canned hook listings."""

    def __init__(self, hooks):
        self.hooks = hooks
        self.calls = []

    def request(self, method, path, body=None, *, ok_status=(200, 201)):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/hooks?per_page=100"):
            return self.hooks
        if method == "POST" and path.endswith("/hooks"):
            return {"id": 4242}
        return {}


def _register_module():
    return _load_module(_REGISTER_REPO, "register_repo_under_test")


def test_reconcile_webhook_creates_then_reconciles():
    module = _register_module()
    args = SimpleNamespace(repo="owner/sample-target-repo", trigger_id="trig", webhook_url="https://api.example.com/webhook")

    fresh = _FakeGitHub(hooks=[])
    assert module.reconcile_webhook(fresh, args, "secret") == 4242
    created = [call for call in fresh.calls if call[0] == "POST"]
    assert created and created[0][2]["config"]["url"] == "https://api.example.com/webhook/trig"

    existing_hook = {"id": 7, "config": {"url": "https://api.example.com/webhook/trig"}}
    existing = _FakeGitHub(hooks=[existing_hook])
    assert module.reconcile_webhook(existing, args, "secret") == 7
    patched = [call for call in existing.calls if call[0] == "PATCH"]
    assert patched and patched[0][1].endswith("/hooks/7")


def test_register_repo_settings_item_matches_shell_registration_shape():
    module = _register_module()

    class _FakeDynamo:
        def __init__(self):
            self.items = []

        def put_item(self, TableName, Item):
            self.items.append((TableName, Item))

    args = SimpleNamespace(
        repo="owner/sample-target-repo",
        trigger_id="trig",
        git_url="https://github.com/owner/sample-target-repo.git",
        github_token_ssm="/openci-tf/clone-token/owner-sample-target-repo-control",
        upstream_urls_json=json.dumps(
            {"tofu:1.9.0": "https://github.com/opentofu/opentofu/releases/download/v1.9.0/tofu_1.9.0_linux_amd64.tar.gz"}
        ),
        region="us-east-1",
        table="openci-tf-settings",
        infracost_api_key_ssm="",
        require_approval=False,
    )
    dynamo = _FakeDynamo()
    module.apply_repo_settings(dynamo, args, "/openci-tf/install/openci-tf/webhook_secret")
    (table, item), = dynamo.items
    assert table == "openci-tf-settings"
    assert item["pk"] == {"S": "repo"}
    assert item["sk"] == {"S": "trig"}
    assert item["webhook_secret_ssm"] == {"S": "/openci-tf/install/openci-tf/webhook_secret"}
    assert item["upstream_urls"]["M"]["tofu:1.9.0"]["S"].startswith("https://")


def test_register_repo_rejects_unpinned_upstream_urls():
    module = _register_module()
    args = SimpleNamespace(
        repo="owner/sample-target-repo",
        trigger_id="trig",
        git_url="https://github.com/owner/sample-target-repo.git",
        github_token_ssm="/openci-tf/clone-token/owner-sample-target-repo-control",
        upstream_urls_json=json.dumps({"tofu:9.9.9": "https://example.com/x.tar.gz"}),
        region="us-east-1",
        table="openci-tf-settings",
        infracost_api_key_ssm="",
        require_approval=False,
    )
    with pytest.raises(module.RegistrationError):
        module.apply_repo_settings(object(), args, "/openci-tf/install/openci-tf/webhook_secret")
