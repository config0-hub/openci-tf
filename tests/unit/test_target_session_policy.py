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
    target_state_key,
)
from src.services.run_folder import prepare_and_submit
from tests.unit.iam_policy_evaluator import evaluate_inline_policy

_ACCOUNT_ID = "123456789012"
_BUCKET = f"arn:aws:s3:::openci-tf-state-{_ACCOUNT_ID}"
_TABLE = f"arn:aws:dynamodb:us-east-1:{_ACCOUNT_ID}:table/openci-tf-tf-locks"


def _policy(action: str, folder: str = "infra/folder-a") -> tuple[str, dict]:
    rendered = render_target_session_policy(
        account_id=_ACCOUNT_ID,
        repo_name="org/repo",
        folder=folder,
        action=action,
        project_name="openci-tf",
        region="us-east-1",
    )
    return rendered, json.loads(rendered)


def test_state_key_matches_checked_in_backend_layout() -> None:
    assert (
        target_state_key("org/repo", "infra/folder-a")
        == "targets/org/repo/infra/folder-a.tfstate"
    )


def test_mutation_policy_contains_only_the_exact_folder_backend_arns() -> None:
    rendered, policy = _policy("apply")
    state_key = "targets/org/repo/infra/folder-a.tfstate"
    state_arn = f"{_BUCKET}/{state_key}"
    lock_id = f"openci-tf-state-{_ACCOUNT_ID}/{state_key}"

    exact_state = next(
        statement
        for statement in policy["Statement"]
        if statement["Sid"] == "ExactStateObject"
    )
    exact_list = next(
        statement
        for statement in policy["Statement"]
        if statement["Sid"] == "ExactStateBucketList"
    )
    lock_write = next(
        statement
        for statement in policy["Statement"]
        if statement["Sid"] == "ExactStateLockWrite"
    )

    assert exact_state["Resource"] == state_arn
    assert exact_state["Action"] == ["s3:GetObject", "s3:PutObject"]
    assert exact_list["Resource"] == _BUCKET
    assert exact_list["Condition"]["StringEquals"]["s3:prefix"] == state_key
    assert lock_write["Resource"] == _TABLE
    assert lock_write["Condition"]["ForAllValues:StringEquals"][
        "dynamodb:LeadingKeys"
    ] == [lock_id, f"{lock_id}-md5"]
    assert "folder-a*" not in rendered
    assert len(rendered) <= MAX_SESSION_POLICY_CHARS


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
        action="dynamodb:PutItem",
        resource=_TABLE,
        context={
            "dynamodb:LeadingKeys": [
                f"openci-tf-state-{_ACCOUNT_ID}/{folder_b_key}"
            ]
        },
    )


def test_read_session_has_no_state_object_write() -> None:
    _, policy = _policy("plan")
    exact_state = next(
        statement
        for statement in policy["Statement"]
        if statement["Sid"] == "ExactStateObject"
    )
    assert exact_state["Action"] == ["s3:GetObject"]
    assert not evaluate_inline_policy(
        policy,
        action="s3:PutObject",
        resource=f"{_BUCKET}/targets/org/repo/infra/folder-a.tfstate",
    )


def test_read_session_allows_only_the_plan_lock_write_key() -> None:
    _, policy = _policy("plan")
    lock_id = (
        f"openci-tf-state-{_ACCOUNT_ID}/"
        "targets/org/repo/infra/folder-a.tfstate"
    )
    assert evaluate_inline_policy(
        policy,
        action="dynamodb:PutItem",
        resource=_TABLE,
        context={"dynamodb:LeadingKeys": [lock_id]},
    )
    assert not evaluate_inline_policy(
        policy,
        action="dynamodb:PutItem",
        resource=_TABLE,
        context={"dynamodb:LeadingKeys": [f"{lock_id}-md5"]},
    )


def test_policy_interpolation_reuses_folder_validation_and_rejects_globs() -> None:
    with pytest.raises(ValueError, match="invalid folder path"):
        target_state_key("org/repo", "../other")
    with pytest.raises(ConfigResolutionError, match="wildcard"):
        target_state_key("org/repo", "infra/*")


def test_session_policy_limit_fails_loud() -> None:
    with pytest.raises(ConfigResolutionError, match="2048-character"):
        _policy("apply", "folder-" + "a" * 100)


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
