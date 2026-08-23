"""Fail-closed phase-0 security and validation contracts."""

import base64
import json
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from src.core.errors import ConfigValidationError, LockHeldError
from src.core.models import RepoSettings
from src.domain.accounts.aliases import load_account_alias
from src.domain.engine.payload import EnginePayload
from src.domain.locks.run_lock import acquire, release
from src.services.webhook.handler import handler


def _payload(**overrides: object) -> EnginePayload:
    fields: dict[str, str] = {
        "trigger_id": "trigger", "s3_package_uri": "s3://bucket/package",
        "sops_type": "kms", "sops_path": "secrets.enc",
        "commands_b64": base64.b64encode(b"plan").decode(),
        "done_endpoint": "s3://bucket/done", "execution_target": "lambda",
        "timeout_seconds": 900,
    }
    fields.update(overrides)
    return EnginePayload(**fields)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"trigger_id": ""}, "required"),
        ({"s3_package_uri": "https://bucket/package"}, "s3 URIs"),
        ({"execution_target": "ecs"}, "unknown execution target"),
        ({"commands_b64": "!not-base64!"}, "base64"),
    ],
)
def test_engine_payload_rejects_each_guard(override: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _payload(**override).validate()


class _LockTable:
    """Small DynamoDB-condition evaluator for lock contract tests."""

    def __init__(self, item: dict[str, object] | None = None) -> None:
        self.item = item
        self.put_kwargs: dict[str, object] | None = None
        self.delete_kwargs: dict[str, object] | None = None

    def put_item(self, **kwargs: object) -> None:
        self.put_kwargs = kwargs
        now = kwargs["ExpressionAttributeValues"][":now"]  # type: ignore[index]
        condition = kwargs["ConditionExpression"]
        if self.item is not None:
            expires_at = self.item["expires_at"]
            condition_met = (
                "expires_at < :now" in condition and expires_at < now
            ) or ("expires_at > :now" in condition and expires_at > now)
            if not condition_met:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
                )
        self.item = kwargs["Item"]  # type: ignore[assignment]

    def get_item(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"Item": self.item} if self.item is not None else {}

    def delete_item(self, **kwargs: object) -> None:
        self.delete_kwargs = kwargs
        holder = kwargs["ExpressionAttributeValues"][":holder"]  # type: ignore[index]
        condition = kwargs["ConditionExpression"]
        if self.item is None or not (
            "holder_execution_id = :holder" in condition
            and self.item["holder_execution_id"] == holder
        ):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem"
            )
        self.item = None


def test_lock_expiry_reacquire_and_non_holder_release() -> None:
    expired = _LockTable({"holder_execution_id": "old", "expires_at": 99})
    acquire(expired, "org/repo", "infra/vpc", "new", now=100, ttl=60)
    assert expired.put_kwargs is not None
    assert expired.put_kwargs["ConditionExpression"] == (
        "attribute_not_exists(pk) OR expires_at < :now"
    )
    assert expired.put_kwargs["ExpressionAttributeValues"] == {":now": 100}

    held = _LockTable({"holder_execution_id": "old", "expires_at": 101})
    with pytest.raises(LockHeldError, match="old"):
        acquire(held, "org/repo", "infra/vpc", "new", now=100, ttl=60)

    release(held, "org/repo", "infra/vpc", "other")
    assert held.delete_kwargs is not None
    assert held.delete_kwargs["ConditionExpression"] == "holder_execution_id = :holder"
    assert held.delete_kwargs["ExpressionAttributeValues"] == {":holder": "other"}


@patch("src.domain.accounts.aliases.get_account_alias")
def test_account_alias_loader_contract(get_account_alias: Mock) -> None:
    get_account_alias.return_value = {"account_id": "123456789012", "role_name": "openci-tf-target"}
    assert load_account_alias("prod").account_id == "123456789012"
    get_account_alias.return_value = {"account_id": "123456789012"}
    assert load_account_alias("prod").role_name == "openci-tf-executor-remote"
    get_account_alias.side_effect = ValueError("Unknown account alias: 'missing'")
    with pytest.raises(ConfigValidationError, match="Unknown account alias"):
        load_account_alias("missing")
    get_account_alias.side_effect = None
    get_account_alias.return_value = {"account_id": "invalid", "role_name": "bad role!"}
    with pytest.raises(ConfigValidationError, match="account_id"):
        load_account_alias("malformed")


@patch("src.domain.accounts.aliases.get_account_alias")
def test_account_alias_loader_accepts_dynamodb_decimal_max_ttl(get_account_alias: Mock) -> None:
    get_account_alias.return_value = {
        "account_id": "123456789012",
        "role_name": "openci-tf-target",
        "max_ttl": Decimal(3600),
    }
    assert load_account_alias("prod").max_ttl == 3600


@patch("src.domain.accounts.aliases.get_account_alias")
def test_account_alias_loader_enable_apply_defaults_false(get_account_alias: Mock) -> None:
    get_account_alias.return_value = {"account_id": "123456789012", "role_name": "openci-tf-target"}
    assert load_account_alias("prod").enable_apply is False


@patch("src.domain.accounts.aliases.get_account_alias")
def test_account_alias_loader_enable_apply_parses_true(get_account_alias: Mock) -> None:
    get_account_alias.return_value = {
        "account_id": "123456789012",
        "role_name": "openci-tf-target",
        "enable_apply": True,
    }
    assert load_account_alias("prod").enable_apply is True


@patch("src.domain.accounts.aliases.get_account_alias")
def test_account_alias_loader_poweruser_role_defaults_when_enable_apply(get_account_alias: Mock) -> None:
    get_account_alias.return_value = {
        "account_id": "123456789012",
        "role_name": "openci-tf-target",
        "enable_apply": True,
    }
    alias = load_account_alias("prod")
    assert alias.poweruser_role_name == "openci-tf-executor-poweruser"
    assert alias.enable_apply is True


@patch("src.domain.accounts.aliases.get_account_alias")
def test_account_alias_loader_poweruser_role_absent_when_disabled(get_account_alias: Mock) -> None:
    get_account_alias.return_value = {
        "account_id": "123456789012",
        "role_name": "openci-tf-target",
        "enable_apply": False,
    }
    assert load_account_alias("prod").poweruser_role_name is None


SETTINGS = RepoSettings(
    trigger_id="trigger", repo_name="org/repo", git_url="https://github.com/org/repo.git",
    secret="secret", ssm_openci_tf_github_token="/openci-tf/clone-token/test",
)


def _event() -> dict[str, object]:
    return {
        "trigger_id": "trigger", "headers": {"X-GitHub-Event": "issue_comment"},
        "body": json.dumps({
            "action": "created", "comment": {"body": "tf plan infra/vpc", "user": {"login": "alice"}},
            "issue": {"number": 1, "pull_request": {"url": "https://api.github.test/pr/1"}},
            "repository": {"full_name": "org/repo"},
        }),
    }


@patch("src.services.webhook.handler.start_run_from_request")
@patch("src.services.webhook.handler.get_collaborator_permission")
@patch("src.services.webhook.handler.get_pull_request")
@patch("src.services.webhook.handler.get_github_token", return_value="token")
@patch("src.services.webhook.handler.get_repo_settings", return_value=SETTINGS)
def test_webhook_refuses_permission_missing_sha_and_fork(
    get_settings: Mock, get_token: Mock, get_pr: Mock, permission: Mock, start_run: Mock,
) -> None:
    del get_settings, get_token
    permission.return_value = "read"
    get_pr.return_value = {"head": {"sha": "abc", "repo": {"full_name": "org/repo"}}, "base": {"repo": {"full_name": "org/repo"}}}
    response = handler(_event(), None)
    assert response["statusCode"] == 403
    start_run.assert_not_called()

    permission.reset_mock()
    get_pr.return_value = {"head": {"repo": {"full_name": "org/repo"}}, "base": {"repo": {"full_name": "org/repo"}}}
    response = handler(_event(), None)
    assert response["statusCode"] == 422
    permission.assert_not_called()

    get_pr.return_value = {"head": {"sha": "abc", "repo": {"full_name": "fork/repo"}}, "base": {"repo": {"full_name": "org/repo"}}}
    response = handler(_event(), None)
    assert response["statusCode"] == 403
    permission.assert_not_called()


@patch("src.services.webhook.handler.start_run_from_request")
@patch("src.services.webhook.handler.get_collaborator_permission")
@patch("src.services.webhook.handler.get_github_token", return_value="token")
@patch("src.services.webhook.handler.get_repo_settings", return_value=SETTINGS)
def test_webhook_refuses_repository_identity_mismatch(
    get_settings: Mock, get_token: Mock, permission: Mock, start_run: Mock,
) -> None:
    del get_settings, get_token
    event = _event()
    payload = json.loads(event["body"])
    payload["repository"]["full_name"] = "other/repo"
    event["body"] = json.dumps(payload)
    response = handler(event, None)
    assert response["statusCode"] == 403
    permission.assert_not_called()
    start_run.assert_not_called()
