# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-folder target session policy and frozen account binding tests."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.core.errors import ConfigResolutionError
from src.domain.accounts.target_session import (
    MAX_SESSION_POLICY_CHARS,
    render_target_session_policy,
    resolve_effective_state_location,
    target_state_key,
)
from src.services.run_folder import prepare_and_submit
from tests.unit.iam_policy_evaluator import evaluate_inline_policy

_ACCOUNT_ID = "123456789012"
_BUCKET = f"arn:aws:s3:::openci-tf-state-{_ACCOUNT_ID}"
_LONGEST_GITOPS_FOLDER = (
    "terraform/secondary/ap-northeast-1/04-cloudwatch-log-group"
)


def _statement_with_action(policy: dict, action: str) -> dict:
    for statement in policy["Statement"]:
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if action in actions:
            return statement
    raise AssertionError(f"no statement grants Action {action!r}")


def _state_object_statement(policy: dict) -> dict:
    for statement in policy["Statement"]:
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if not any(action.startswith("s3:") for action in actions):
            continue
        resource = statement.get("Resource", "")
        if isinstance(resource, str) and resource.endswith(".tfstate"):
            return statement
    raise AssertionError("no statement grants exact state object access")


def _lock_object_statement(policy: dict) -> dict:
    for statement in policy["Statement"]:
        resource = statement.get("Resource", "")
        if isinstance(resource, str) and resource.endswith(".tflock"):
            return statement
    raise AssertionError("no statement grants exact lock object access")


def _policy(
    action: str,
    folder: str = "infra/folder-a",
    *,
    state_bucket: str = "",
    state_key: str = "",
) -> tuple[str, dict]:
    rendered = render_target_session_policy(
        account_id=_ACCOUNT_ID,
        repo_name="org/repo",
        folder=folder,
        action=action,
        project_name="openci-tf",
        state_bucket=state_bucket,
        state_key=state_key,
    )
    return rendered, json.loads(rendered)


def test_state_key_matches_checked_in_backend_layout() -> None:
    assert (
        target_state_key("org/repo", "infra/folder-a")
        == "targets/org/repo/infra/folder-a.tfstate"
    )


def test_effective_state_location_defaults_to_conventional_pair() -> None:
    assert resolve_effective_state_location(
        account_id=_ACCOUNT_ID,
        repo_name="org/repo",
        folder="infra/folder-a",
        project_name="openci-tf",
    ) == (
        f"openci-tf-state-{_ACCOUNT_ID}",
        "targets/org/repo/infra/folder-a.tfstate",
    )


def test_effective_state_location_uses_folder_override_verbatim() -> None:
    assert resolve_effective_state_location(
        account_id=_ACCOUNT_ID,
        repo_name="org/repo",
        folder="infra/folder-a",
        project_name="openci-tf",
        state_bucket="tenant-state-bucket",
        state_key="targets/org/repo/primary/123456789012/us-east-1/p/s/e/i/terraform.tfstate",
    ) == (
        "tenant-state-bucket",
        "targets/org/repo/primary/123456789012/us-east-1/p/s/e/i/terraform.tfstate",
    )


def test_effective_state_location_rejects_lone_override_half() -> None:
    for kwargs in (
        {"state_bucket": "tenant-state-bucket"},
        {"state_key": "targets/x/terraform.tfstate"},
    ):
        with pytest.raises(ConfigResolutionError, match="set together"):
            resolve_effective_state_location(
                account_id=_ACCOUNT_ID,
                repo_name="org/repo",
                folder="infra/folder-a",
                project_name="openci-tf",
                **kwargs,
            )


def test_effective_state_location_rejects_glob_key_and_bad_bucket() -> None:
    with pytest.raises(ConfigResolutionError, match="wildcard"):
        resolve_effective_state_location(
            account_id=_ACCOUNT_ID,
            repo_name="org/repo",
            folder="infra/folder-a",
            project_name="openci-tf",
            state_bucket="tenant-state-bucket",
            state_key="targets/*",
        )
    with pytest.raises(ConfigResolutionError, match="state_bucket"):
        resolve_effective_state_location(
            account_id=_ACCOUNT_ID,
            repo_name="org/repo",
            folder="infra/folder-a",
            project_name="openci-tf",
            state_bucket="Bad_Bucket*",
            state_key="targets/x/terraform.tfstate",
        )


def test_mutation_policy_contains_only_the_exact_folder_backend_arns() -> None:
    rendered, policy = _policy("apply")
    state_key = "targets/org/repo/infra/folder-a.tfstate"
    state_arn = f"{_BUCKET}/{state_key}"

    exact_state = _state_object_statement(policy)
    exact_list = _statement_with_action(policy, "s3:ListBucket")
    lock_object = _lock_object_statement(policy)

    assert exact_state["Resource"] == state_arn
    assert exact_state["Action"] == ["s3:GetObject", "s3:PutObject"]
    assert exact_list["Resource"] == _BUCKET
    assert exact_list["Condition"]["StringEquals"]["s3:prefix"] == [
        state_key,
        f"{state_key}.tflock",
    ]
    assert lock_object["Resource"] == f"{state_arn}.tflock"
    assert lock_object["Action"] == [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    ]
    assert "folder-a*" not in rendered
    assert len(rendered) <= MAX_SESSION_POLICY_CHARS


def test_policy_contains_no_dynamodb_authority() -> None:
    for action in ("plan", "apply", "destroy", "drift", "report", "plan_destroy"):
        rendered, _ = _policy(action)
        assert "dynamodb" not in rendered.lower()


def test_folder_a_session_does_not_allow_folder_b_state_or_lock() -> None:
    _, policy = _policy("apply")
    folder_b_key = "targets/org/repo/infra/folder-b.tfstate"

    assert not evaluate_inline_policy(
        policy,
        action="s3:GetObject",
        resource=f"{_BUCKET}/{folder_b_key}",
    )
    assert not evaluate_inline_policy(
        policy,
        action="s3:PutObject",
        resource=f"{_BUCKET}/{folder_b_key}.tflock",
    )


def test_read_session_has_no_state_object_write_but_locks() -> None:
    _, policy = _policy("plan")
    state_key = "targets/org/repo/infra/folder-a.tfstate"
    exact_state = _state_object_statement(policy)
    assert exact_state["Action"] == ["s3:GetObject"]
    assert not evaluate_inline_policy(
        policy,
        action="s3:PutObject",
        resource=f"{_BUCKET}/{state_key}",
    )
    # The S3 native lock file is created and removed by every locked run, so
    # the read lane may write the lock object (and only the lock object).
    for lock_action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject"):
        assert evaluate_inline_policy(
            policy,
            action=lock_action,
            resource=f"{_BUCKET}/{state_key}.tflock",
        )


def test_override_policy_scopes_to_effective_pair_only() -> None:
    override_bucket = "tenant-state-bucket"
    override_key = (
        "targets/org/repo/primary/123456789012/us-east-1/p/s/e/i/terraform.tfstate"
    )
    rendered, policy = _policy(
        "apply", state_bucket=override_bucket, state_key=override_key
    )
    bucket_arn = f"arn:aws:s3:::{override_bucket}"

    exact_state = _state_object_statement(policy)
    assert exact_state["Resource"] == f"{bucket_arn}/{override_key}"
    assert _lock_object_statement(policy)["Resource"] == (
        f"{bucket_arn}/{override_key}.tflock"
    )
    # The conventional per-account key is NOT reachable under an override.
    assert not evaluate_inline_policy(
        policy,
        action="s3:GetObject",
        resource=f"{bucket_arn}/targets/org/repo/infra/folder-a.tfstate",
    )
    workload = next(
        statement
        for statement in policy["Statement"]
        if statement.get("Action") == "*" and "NotResource" in statement
    )
    assert workload["NotResource"] == [f"{bucket_arn}*"]
    assert len(rendered) <= MAX_SESSION_POLICY_CHARS


def test_policy_interpolation_reuses_folder_validation_and_rejects_globs() -> None:
    with pytest.raises(ValueError, match="invalid folder path"):
        target_state_key("org/repo", "../other")
    with pytest.raises(ConfigResolutionError, match="wildcard"):
        target_state_key("org/repo", "infra/*")


def test_session_policy_limit_fails_loud() -> None:
    with pytest.raises(ConfigResolutionError, match="1800-character"):
        _policy(
            "apply",
            state_bucket="tenant-state-bucket",
            state_key="targets/" + "a" * 400 + "/terraform.tfstate",
        )


def test_policy_has_no_sid_fields() -> None:
    _, policy = _policy("apply")
    for statement in policy["Statement"]:
        assert "Sid" not in statement


def test_workload_authority_uses_wildcard_not_resources() -> None:
    _, policy = _policy("apply")
    workload = next(
        statement
        for statement in policy["Statement"]
        if statement.get("Action") == "*" and "NotResource" in statement
    )
    assert workload["NotResource"] == [f"{_BUCKET}*"]


def test_longest_gitops_folder_policy_stays_under_packed_quota_limit() -> None:
    rendered = render_target_session_policy(
        account_id=_ACCOUNT_ID,
        repo_name="williaumwu/openci-test-gitops",
        folder=_LONGEST_GITOPS_FOLDER,
        action="apply",
        project_name="openci-tf",
    )
    assert len(rendered) <= MAX_SESSION_POLICY_CHARS


def test_decision_26_shaped_override_policy_stays_under_packed_quota_limit() -> None:
    rendered = render_target_session_policy(
        account_id=_ACCOUNT_ID,
        repo_name="williaumwu/openci-test-gitops",
        folder=_LONGEST_GITOPS_FOLDER,
        action="apply",
        project_name="openci-tf",
        state_bucket="config0-tenant-state-bucket-123456789012",
        state_key=(
            "targets/williaumwu/openci-test-gitops/primary/123456789012/"
            "ap-northeast-1/sample-project/sample-stack/cloudwatch-log-group/"
            "stateful-0001/terraform.tfstate"
        ),
    )
    assert len(rendered) <= MAX_SESSION_POLICY_CHARS


def test_unregistered_state_pair_refuses_run(monkeypatch) -> None:
    monkeypatch.setattr(
        prepare_and_submit,
        "get_allowed_state_pairs",
        lambda repo_name: frozenset({"tenant-state-bucket/registered/terraform.tfstate"}),
    )
    with pytest.raises(ValueError, match="not registered in allowed_state_pairs"):
        prepare_and_submit._require_allowed_state_pair(
            repo_name="org/repo",
            state_bucket="tenant-state-bucket",
            state_key="other/terraform.tfstate",
        )


def test_registered_state_pair_is_accepted(monkeypatch) -> None:
    monkeypatch.setattr(
        prepare_and_submit,
        "get_allowed_state_pairs",
        lambda repo_name: frozenset({"tenant-state-bucket/registered/terraform.tfstate"}),
    )
    prepare_and_submit._require_allowed_state_pair(
        repo_name="org/repo",
        state_bucket="tenant-state-bucket",
        state_key="registered/terraform.tfstate",
    )


def test_assumed_identity_mismatch_refuses_execution(monkeypatch) -> None:
    credentials = {
        "AWS_ACCESS_KEY_ID": "id",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_SESSION_TOKEN": "token",
    }
    monkeypatch.setattr(
        prepare_and_submit.sts,
        "assume_role",
        lambda *_args, **_kwargs: credentials,
    )
    monkeypatch.setattr(
        prepare_and_submit.sts,
        "get_caller_account_id",
        lambda supplied=None: "210987654321" if supplied else "999999999999",
    )

    with pytest.raises(ValueError, match="expected 123456789012, got 210987654321"):
        prepare_and_submit._assume_target_role(
            "arn:aws:iam::123456789012:role/openci-tf-executor-readonly",
            "execution",
            900,
            3600,
            "openci-tf-0123456789abcdef",
            "{}",
            "123456789012",
        )


def test_submit_path_does_not_resolve_account_alias_again() -> None:
    source = Path("src/services/run_folder/prepare_and_submit.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "load_account_alias" not in imports
    assert "load_account_alias" not in calls
