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
    ecr = recipe.find("run_stage addon-ecr")
    copy = recipe.find("run_stage addon-image-copy")
    deploy = recipe.find("run_stage addon-deploy")
    register = recipe.find("run_stage addon-register")
    assert -1 not in (ecr, copy, deploy, register)
    assert ecr < copy < deploy < register
    assert "--stage ecr" in recipe
    assert "--stage deploy" in recipe
    assert "copy_ghcr_image.sh" in recipe
    assert "register_repo.py" in recipe
    assert "tf-locks" not in recipe


def test_install_config0_addon_journey_stops_at_first_failed_stage(tmp_path):
    """A failing ecr stage never reaches image copy, deploy, or registration."""
    recipe = _addon_recipe()
    body = "\n".join(
        line[4:]
        for line in recipe.splitlines()
        if line.startswith("    ")
    )
    body = body.replace("{{OPENCI_TF_REGION}}", "us-east-1").replace("{{OPENCI_TF_PROJECT}}", "openci-tf")

    (tmp_path / "scripts").mkdir()
    (tmp_path / "install").mkdir()
    (tmp_path / "scripts/phase_timing.sh").write_text(
        (_REPO_ROOT / "scripts/phase_timing.sh").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "scripts/ssm_config.sh").write_text('#!/usr/bin/env bash\necho "stub-value"\n', encoding="utf-8")
    (tmp_path / "scripts/copy_ghcr_image.sh").write_text(
        '#!/usr/bin/env bash\ntouch copy-ran\n', encoding="utf-8"
    )
    (tmp_path / "install/config0_addon.py").write_text(
        "import sys\n"
        "if 'ecr' in sys.argv:\n"
        "    sys.exit(3)\n"
        "open('deploy-ran', 'w').close()\n",
        encoding="utf-8",
    )
    (tmp_path / "install/register_repo.py").write_text(
        "open('register-ran', 'w').close()\n", encoding="utf-8"
    )
    for script in ("scripts/ssm_config.sh", "scripts/copy_ghcr_image.sh"):
        (tmp_path / script).chmod(0o755)

    completed = subprocess.run(
        ["bash", "-c", body],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 3, completed.stderr
    assert "stopped at failed stage addon-ecr" in completed.stderr
    assert not (tmp_path / "copy-ran").exists()
    assert not (tmp_path / "deploy-ran").exists()
    assert not (tmp_path / "register-ran").exists()


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
    assert "use_lock_table" not in data
    assert "aws_dynamodb_table" not in data
    assert "lock_table_arn" not in main
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
    assert module.reconcile_webhook(fresh, args, "secret") == (4242, True, None)
    created = [call for call in fresh.calls if call[0] == "POST"]
    assert created and created[0][2]["config"]["url"] == "https://api.example.com/webhook/trig"

    existing_hook = {
        "id": 7,
        "active": False,
        "events": ["push"],
        "config": {"url": "https://api.example.com/webhook/trig", "content_type": "form"},
    }
    existing = _FakeGitHub(hooks=[existing_hook])
    assert module.reconcile_webhook(existing, args, "secret") == (
        7,
        False,
        {
            "active": False,
            "events": ["push"],
            "config": {"url": "https://api.example.com/webhook/trig", "content_type": "form"},
        },
    )
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
            {"tofu:1.12.6": "https://github.com/opentofu/opentofu/releases/download/v1.12.6/tofu_1.12.6_linux_amd64.tar.gz"}
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
    assert item["upstream_urls"]["M"]["tofu:1.12.6"]["S"].startswith("https://")


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


class _FakeSsm:
    def __init__(self, parameters=None, fail_put=False):
        self.parameters = dict(parameters or {})
        self.fail_put = fail_put
        self.exceptions = SimpleNamespace(ParameterNotFound=KeyError)

    def get_parameter(self, Name, WithDecryption=False):
        if Name not in self.parameters:
            raise KeyError(Name)
        return {"Parameter": {"Value": self.parameters[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite=False):
        if self.fail_put:
            raise RuntimeError("ssm put_parameter failed")
        self.parameters[Name] = Value


class _FakeDynamoDb:
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.calls = []

    @staticmethod
    def _key(key_or_item):
        return (key_or_item["pk"]["S"], key_or_item["sk"]["S"])

    def get_item(self, TableName, Key):
        self.calls.append(("get_item", Key))
        item = self.items.get(self._key(Key))
        return {"Item": item} if item is not None else {}

    def put_item(self, TableName, Item):
        self.calls.append(("put_item", Item))
        self.items[self._key(Item)] = Item

    def delete_item(self, TableName, Key):
        self.calls.append(("delete_item", Key))
        self.items.pop(self._key(Key), None)


def _register_args(**overrides):
    args = SimpleNamespace(
        repo="owner/sample-target-repo",
        trigger_id="trig",
        git_url="https://github.com/owner/sample-target-repo.git",
        github_token_ssm="/openci-tf/clone-token/owner-sample-target-repo-control",
        webhook_url="https://api.example.com/webhook",
        upstream_urls_json=json.dumps(
            {"tofu:1.12.6": "https://github.com/opentofu/opentofu/releases/download/v1.12.6/tofu_1.12.6_linux_amd64.tar.gz"}
        ),
        region="us-east-1",
        project_name="openci-tf",
        table="openci-tf-settings",
        infracost_api_key_ssm="",
        require_approval=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_main_runs_comment_probe_before_any_activation(monkeypatch):
    """The probe must pass before the settings row, webhook, or hook id are written."""
    module = _register_module()
    order = []
    monkeypatch.setattr(module, "comment_probe", lambda github, args: order.append("probe"))
    monkeypatch.setattr(module, "get_or_create_secret", lambda ssm, path: (order.append("secret"), "s")[1])
    monkeypatch.setattr(module, "apply_repo_settings", lambda dynamodb, args, path: order.append("settings"))
    monkeypatch.setattr(module, "reconcile_webhook", lambda github, args, secret: (order.append("webhook"), (1, True, None))[1])
    monkeypatch.setattr(module, "record_hook_id", lambda ssm, args, hook_id: (order.append("record"), "p")[1])

    fake_boto3 = SimpleNamespace(
        client=lambda name, region_name: _FakeSsm(
            {"/openci-tf/clone-token/owner-sample-target-repo-control": "token"}
        )
        if name == "ssm"
        else _FakeDynamoDb()
    )
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    rc = module.main(
        [
            "--repo", "owner/sample-target-repo",
            "--trigger-id", "trig",
            "--webhook-url", "https://api.example.com/webhook",
            "--upstream-urls-json", json.dumps({"tofu:1.12.6": "https://example.com/tofu.tar.gz"}),
            "--region", "us-east-1",
        ]
    )
    assert rc == 0
    assert order == ["probe", "secret", "settings", "webhook", "record"]


def test_main_failed_probe_never_activates(monkeypatch):
    module = _register_module()
    touched = []
    monkeypatch.setattr(
        module,
        "comment_probe",
        lambda github, args: (_ for _ in ()).throw(module.RegistrationError("probe failed")),
    )
    monkeypatch.setattr(module, "apply_repo_settings", lambda *a: touched.append("settings"))
    monkeypatch.setattr(module, "reconcile_webhook", lambda *a: touched.append("webhook"))
    monkeypatch.setattr(module, "record_hook_id", lambda *a: touched.append("record"))
    fake_boto3 = SimpleNamespace(
        client=lambda name, region_name: _FakeSsm(
            {"/openci-tf/clone-token/owner-sample-target-repo-control": "token"}
        )
        if name == "ssm"
        else _FakeDynamoDb()
    )
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    with pytest.raises(module.RegistrationError, match="probe failed"):
        module.main(
            [
                "--repo", "owner/sample-target-repo",
                "--trigger-id", "trig",
                "--webhook-url", "https://api.example.com/webhook",
                "--upstream-urls-json", json.dumps({"tofu:1.12.6": "https://example.com/tofu.tar.gz"}),
                "--region", "us-east-1",
            ]
        )
    assert touched == []


def test_activate_registration_rolls_back_on_late_failure():
    """record_hook_id failing removes the new hook and the fresh settings row."""
    module = _register_module()
    args = _register_args()
    github = _FakeGitHub(hooks=[])
    ssm = _FakeSsm(fail_put=True)
    dynamodb = _FakeDynamoDb()

    with pytest.raises(RuntimeError, match="ssm put_parameter failed"):
        module.activate_registration(
            github, ssm, dynamodb, args, "secret", "/openci-tf/install/openci-tf/webhook_secret"
        )
    deletes = [call for call in github.calls if call[0] == "DELETE"]
    assert deletes and deletes[0][1].endswith("/hooks/4242")
    assert ("delete_item", {"pk": {"S": "repo"}, "sk": {"S": "trig"}}) in dynamodb.calls
    assert ("repo", "trig") not in dynamodb.items


def test_activate_registration_restores_prior_settings_row_on_late_failure():
    module = _register_module()
    args = _register_args()
    prior = {"pk": {"S": "repo"}, "sk": {"S": "trig"}, "repo_name": {"S": "owner/old-repo"}}
    prior_hook = {
        "id": 7,
        "active": False,
        "events": ["push"],
        "config": {"url": "https://api.example.com/webhook/trig", "content_type": "form"},
    }
    github = _FakeGitHub(hooks=[prior_hook])
    ssm = _FakeSsm(fail_put=True)
    dynamodb = _FakeDynamoDb({("repo", "trig"): prior})

    with pytest.raises(RuntimeError, match="ssm put_parameter failed"):
        module.activate_registration(
            github, ssm, dynamodb, args, "secret", "/openci-tf/install/openci-tf/webhook_secret"
        )
    # Pre-existing hook is reconciled, never deleted.
    assert not [call for call in github.calls if call[0] == "DELETE"]
    # The pre-existing hook is patched back to its prior active flag, events, and config.
    patches = [call for call in github.calls if call[0] == "PATCH" and call[1].endswith("/hooks/7")]
    assert len(patches) == 2
    restore_body = patches[-1][2]
    assert restore_body == {
        "active": False,
        "events": ["push"],
        "config": {"url": "https://api.example.com/webhook/trig", "content_type": "form"},
    }
    assert dynamodb.items[("repo", "trig")] == prior


def test_reconcile_webhook_snapshot_round_trips_prior_state():
    """Patching the snapshot back yields exactly the hook's pre-reconcile fields."""
    module = _register_module()
    args = SimpleNamespace(repo="owner/sample-target-repo", trigger_id="trig", webhook_url="https://api.example.com/webhook")
    prior_fields = {
        "active": False,
        "events": ["push", "release"],
        "config": {"url": "https://api.example.com/webhook/trig", "content_type": "form", "insecure_ssl": "1"},
    }
    github = _FakeGitHub(hooks=[{"id": 7, **prior_fields}])

    hook_id, created, snapshot = module.reconcile_webhook(github, args, "secret")
    assert (hook_id, created) == (7, False)
    assert snapshot == prior_fields

    github.request("PATCH", f"/repos/{args.repo}/hooks/{hook_id}", snapshot)
    restore_body = github.calls[-1][2]
    assert restore_body == prior_fields


class _ProbeGitHub:
    """Serves the probe flow; optionally fails at one path fragment."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = []

    def request(self, method, path, body=None, *, ok_status=(200, 201)):
        self.calls.append((method, path))
        if self.fail_on and self.fail_on in path and method == "POST":
            raise RuntimeError(f"forced failure at {path}")
        if method == "GET" and path == "/repos/owner/sample-target-repo":
            return {"default_branch": "main"}
        if method == "GET" and "git/ref/heads/main" in path:
            return {"object": {"sha": "abc123"}}
        if method == "POST" and path.endswith("/pulls"):
            return {"number": 5}
        if method == "POST" and "/comments" in path:
            return {"id": 9}
        return {}

    def exists(self, path):
        self.calls.append(("EXISTS", path))
        return None


def test_comment_probe_cleans_up_branch_and_pr_on_failure():
    module = _register_module()
    args = _register_args()
    github = _ProbeGitHub(fail_on="/comments")

    with pytest.raises(RuntimeError, match="forced failure"):
        module.comment_probe(github, args)
    assert ("PATCH", "/repos/owner/sample-target-repo/pulls/5") in github.calls
    assert (
        "DELETE",
        "/repos/owner/sample-target-repo/git/refs/heads/openci-tf-register-probe-trig",
    ) in github.calls
