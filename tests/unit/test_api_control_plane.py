# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focused tests for API control-plane seam."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from src.domain.engine.manifest import BucketSet, ManifestBinding, build_manifest, validate_manifest_schema
from src.domain.formatters.artifacts import execution_artifacts_section, folder_comment
from src.domain.run.api_authorization import (
    ApiAuthorizationError,
    resolve_caller_policy,
)
from src.domain.run.fingerprint import request_fingerprint
from src.domain.run.folder_id import decode_folder_id, encode_folder_id
from src.domain.run.request import RunRequestValidationError, parse_run_request
from src.platform.aws import admin_registry
from src.platform.aws.dynamo_codec import normalize_dynamo_item
from src.platform.aws.run_registry import expire_ttl, is_expired
from src.services.api import handler as api_handler

_FULL_SHA = "a" * 40
_API_EVENT = {
    "requestContext": {
        "routeKey": "POST /runs",
        "authorizer": {"iam": {"userArn": "arn:aws:iam::123456789012:role/test-caller"}},
    }
}


@pytest.fixture(autouse=True)
def _api_policy(monkeypatch):
    monkeypatch.setenv(
        "API_CALLER_POLICY_JSON",
        json.dumps(
            {
                "arn:aws:iam::123456789012:role/test-caller": {
                    "trigger_ids": ["trigger-1"],
                    "actions": ["plan", "drift", "report"],
                    "artifact_classes": ["manifest", "text", "json"],
                    "binary_plan": True,
                    "read_classes": ["admin"],
                }
            }
        ),
    )


@pytest.mark.parametrize("action", ["plan_destroy", "apply", "destroy"])
def test_parse_run_request_rejects_non_api_action(action: str):
    with pytest.raises(RunRequestValidationError, match="API action must be one of"):
        parse_run_request(
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": action,
                "folder_mode": "explicit",
                "folders": ["infra/a"],
                "idempotency_key": "key-12345678",
            }
        )


def test_caller_policy_rejects_unsupported_action(monkeypatch):
    monkeypatch.setenv(
        "API_CALLER_POLICY_JSON",
        json.dumps(
            {
                "arn:aws:iam::123456789012:role/test-caller": {
                    "trigger_ids": ["trigger-1"],
                    "actions": ["plan", "plan_destroy"],
                    "artifact_classes": ["manifest"],
                    "binary_plan": False,
                }
            }
        ),
    )
    with pytest.raises(ApiAuthorizationError, match="unsupported API caller policy actions: plan_destroy"):
        resolve_caller_policy(_API_EVENT)


@pytest.mark.parametrize("action", ["plan_destroy", "apply", "destroy"])
def test_api_create_run_rejects_non_safe_verbs_before_authorization_and_orchestration(
    action: str,
    monkeypatch,
):
    monkeypatch.setenv(
        "API_CALLER_POLICY_JSON",
        json.dumps(
            {
                "arn:aws:iam::123456789012:role/test-caller": {
                    "trigger_ids": ["trigger-1"],
                    "actions": [action],
                    "artifact_classes": ["manifest"],
                    "binary_plan": False,
                }
            }
        ),
    )
    body = {
        "trigger_id": "trigger-1",
        "commit_hash": _FULL_SHA,
        "action": action,
        "folder_mode": "all",
        "idempotency_key": "idem-key-12345678",
    }
    with patch(
        "src.services.api.handler.authorize_create_run",
        side_effect=AssertionError("unsafe API verbs must fail before authorization"),
    ), patch(
        "src.services.api.handler.start_run_from_request",
        side_effect=AssertionError("unsafe API verbs must not reach orchestration"),
    ):
        response = api_handler.handler({**_API_EVENT, "body": json.dumps(body)}, None)
    assert response["statusCode"] == 400


def test_parse_run_request_rejects_forbidden_fields():
    with pytest.raises(RunRequestValidationError, match="unknown request fields"):
        parse_run_request({"trigger_id": "trigger-1", "commit_hash": _FULL_SHA, "action": "plan", "folder_mode": "all", "idempotency_key": "key-12345678", "git_url": "https://evil"})


def test_parse_run_request_rejects_unknown_fields_and_service_affected():
    with pytest.raises(RunRequestValidationError, match="unknown request fields"):
        parse_run_request({"trigger_id": "trigger-1", "commit_hash": _FULL_SHA, "action": "plan", "folder_mode": "all", "idempotency_key": "key-12345678", "extra": 1})
    with pytest.raises(RunRequestValidationError, match="service callers must use explicit or all"):
        parse_run_request({"trigger_id": "trigger-1", "commit_hash": _FULL_SHA, "action": "plan", "folder_mode": "affected", "idempotency_key": "key-12345678"})


def test_parse_run_request_accepts_registry_notification():
    request = parse_run_request(
        {
            "trigger_id": "trigger-1",
            "commit_hash": _FULL_SHA,
            "action": "plan",
            "folder_mode": "explicit",
            "folders": ["infra/a"],
            "idempotency_key": "idem-key-12345678",
            "notification_target": {"type": "registry"},
        }
    )
    assert request.notification_target.type == "registry"
    assert request.folders == ["infra/a"]


def test_parse_run_request_deduplicates_and_caps_folders():
    folders = [f"infra/{index}" for index in range(51)]
    with pytest.raises(RunRequestValidationError, match="exceeds maximum"):
        parse_run_request(
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "plan",
                "folder_mode": "explicit",
                "folders": folders,
                "idempotency_key": "idem-key-12345678",
            }
        )
    request = parse_run_request(
        {
            "trigger_id": "trigger-1",
            "commit_hash": _FULL_SHA,
            "action": "plan",
            "folder_mode": "explicit",
            "folders": ["infra/a", "infra/a", "infra/b"],
            "idempotency_key": "idem-key-12345678",
        }
    )
    assert request.folders == ["infra/a", "infra/b"]


def test_retention_env_absent_uses_default(monkeypatch):
    from src.platform.aws import run_registry

    monkeypatch.delenv("RUN_HISTORY_RETENTION_DAYS", raising=False)
    expected = run_registry.DEFAULT_RUN_HISTORY_RETENTION_DAYS * 86400
    assert expire_ttl(0) == expected


def test_retention_env_valid_override(monkeypatch):
    monkeypatch.setenv("RUN_HISTORY_RETENTION_DAYS", "7")
    assert expire_ttl(0) == 7 * 86400


def test_retention_env_malformed_raises(monkeypatch):
    monkeypatch.setenv("RUN_HISTORY_RETENTION_DAYS", "ninety")
    with pytest.raises(ValueError, match="RUN_HISTORY_RETENTION_DAYS.*'ninety'"):
        expire_ttl(0)


def test_expire_ttl_accepts_decimal_records():
    now = 1_700_000_000
    ttl = expire_ttl(now)
    item = normalize_dynamo_item({"expire_ttl": Decimal(str(ttl))})
    assert item is not None
    assert is_expired(item, now=now) is False
    assert is_expired(item, now=ttl) is True


def test_api_get_run_serializes_decimal_records():
    record = normalize_dynamo_item(
        {
            "run_id": "run123",
            "trigger_id": "trigger-1",
            "status": "running",
            "created_at": Decimal(1700000000),
            "updated_at": Decimal(1700000001),
            "expire_ttl": Decimal(str(expire_ttl(1_700_000_000))),
            "pipeline": None,
            "step_count": None,
        }
    )
    with patch("src.services.api.handler.get_run", return_value=record), patch(
        "src.services.api.handler.authorize_read_run", return_value=object()
    ):
        response = api_handler.handler(
            {
                "routeKey": "GET /runs/{run_id}",
                "pathParameters": {"run_id": "run123"},
                "requestContext": {
                    "routeKey": "GET /runs/{run_id}",
                    "authorizer": _API_EVENT["requestContext"]["authorizer"],
                },
            },
            None,
        )
    body = json.loads(response["body"])
    assert body["created_at"] == 1700000000
    assert isinstance(body["expire_ttl"], int)
    assert "pipeline" not in body
    assert "step_count" not in body


def test_api_get_run_includes_pipeline_metadata_when_present():
    record = {
        "run_id": "run123",
        "trigger_id": "trigger-1",
        "action": "plan",
        "status": "running",
        "pipeline": "data/primary",
        "step_count": 2,
    }
    with patch("src.services.api.handler.get_run", return_value=record), patch(
        "src.services.api.handler.authorize_read_run", return_value=object()
    ):
        response = api_handler.handler(
            {
                "routeKey": "GET /runs/{run_id}",
                "pathParameters": {"run_id": "run123"},
                "requestContext": {
                    "routeKey": "GET /runs/{run_id}",
                    "authorizer": _API_EVENT["requestContext"]["authorizer"],
                },
            },
            None,
        )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["pipeline"] == "data/primary"
    assert body["step_count"] == 2


def test_api_list_folders_returns_step_index_and_orders_by_step():
    records = [
        {"folder": "infra/rds", "status": "succeeded", "step_index": 2},
        {"folder": "infra/vpc", "status": "succeeded"},
        {"folder": "infra/ec2", "status": "succeeded", "step_index": 2},
    ]
    with patch("src.services.api.handler.get_run", return_value={"trigger_id": "trigger-1", "action": "plan"}), patch(
        "src.services.api.handler.authorize_read_run", return_value=object()
    ), patch("src.services.api.handler.list_folder_records", return_value=records):
        response = api_handler.handler(
            {
                "routeKey": "GET /runs/{run_id}/folders",
                "pathParameters": {"run_id": "run123"},
                "requestContext": {
                    "routeKey": "GET /runs/{run_id}/folders",
                    "authorizer": _API_EVENT["requestContext"]["authorizer"],
                },
            },
            None,
        )
    assert response["statusCode"] == 200
    folders = json.loads(response["body"])["folders"]
    assert [(item["folder"], item["step_index"]) for item in folders] == [
        ("infra/vpc", 1),
        ("infra/ec2", 2),
        ("infra/rds", 2),
    ]
    assert all(item["folder_id"] == encode_folder_id(item["folder"]) for item in folders)


def test_build_manifest_uses_existing_objects_only():
    import hashlib

    from src.domain.engine.artifact_paths import expected_plan_artifact_uris

    run_id = "run-abc"
    expected = expected_plan_artifact_uris(
        bucket="tmp",
        repo_name="org/repo",
        run_id=run_id,
        folder_path="infra/a",
    )
    metadata = {
        "repo": "org/repo",
        "run_id": run_id,
        "pinned_sha": _FULL_SHA,
        "account_id": "123456789012",
        "folder": "infra/a",
        "action": "plan",
        "opentofu_runtime": "tofu:1.8.0",
        "created_at": "2026-08-10T00:00:00Z",
        "expires_at": "2026-08-11T00:00:00Z",
        "expires_after_days": 1,
        "plan_s3_uri": expected.plan,
        "sha256_s3_uri": expected.checksum,
        "metadata_s3_uri": expected.metadata,
        "sha256": "",
    }
    plan_body = b"x" * 50
    metadata["sha256"] = hashlib.sha256(plan_body).hexdigest()

    def head(bucket: str, key: str):
        base = {"content_length": 12, "content_type": "text/plain", "last_modified": __import__("datetime").datetime(2026, 8, 10, tzinfo=__import__("datetime").timezone.utc)}
        if key.endswith(("init.out", "validate.out", "tf/plan.out")):
            return base
        if key.endswith(("tfsec.json", "infracost.json")):
            return {**base, "content_type": "application/json"}
        if key.endswith("done"):
            return {"content_length": 4, "content_type": "binary/octet-stream", "last_modified": base["last_modified"]}
        if key.endswith(".zip"):
            return {"content_length": 99, "content_type": "application/octet-stream", "last_modified": base["last_modified"]}
        if key.endswith("plan.tfplan"):
            return {"content_length": 50, "content_type": "application/octet-stream", "last_modified": base["last_modified"]}
        if key.endswith("plan.tfplan.sha256"):
            return {"content_length": 65, "content_type": "text/plain", "last_modified": base["last_modified"]}
        if key.endswith("plan-metadata.json"):
            return {"content_length": 200, "content_type": "application/json", "last_modified": base["last_modified"]}
        return None

    plan_digest = metadata["sha256"]

    def read_bytes(bucket: str, key: str, max_bytes: int):
        if key.endswith("plan.tfplan"):
            body = plan_body
        elif key.endswith("plan.tfplan.sha256"):
            body = f"{plan_digest}\n".encode()
        elif key.endswith("plan-metadata.json"):
            body = b"{}"
        else:
            body = b"artifact-body"
        if len(body) > max_bytes:
            return None
        return body

    manifest = build_manifest(
        execution_id="run.abc.0",
        buckets=BucketSet(
            tmp_bucket="tmp",
            done_bucket="done",
            package_bucket="pkg",
            done_uri="s3://done/run.abc.0/done",
            package_uri="s3://pkg/run.abc.0.zip",
        ),
        binding=ManifestBinding(
            run_id=run_id,
            repo_name="org/repo",
            commit_hash=_FULL_SHA,
            account_id="123456789012",
            folder="infra/a",
            attempt=0,
        ),
        action="plan",
        head_object=head,
        read_object_bytes=read_bytes,
        plan_metadata=metadata,
        plan_dimensions={
            "repo_name": "org/repo",
            "commit_hash": _FULL_SHA,
            "account_id": "123456789012",
            "folder": "infra/a",
            "run_id": run_id,
        },
    )
    validate_manifest_schema(manifest, execution_id="run.abc.0")
    names = {entry["name"] for entry in manifest["entries"]}
    assert "plan.tfplan" in names
    assert "tf/plan.out" in names
    assert "package" in names
    assert "init.out" in names
    assert "validate.out" in names


def test_folder_id_round_trip_nested_paths():
    folder = "infra/example"
    folder_id = encode_folder_id(folder)
    assert decode_folder_id(folder_id) == folder


def test_folder_id_rejects_traversal_strings():
    with pytest.raises(ValueError):
        encode_folder_id("../traversal")
    with pytest.raises(ValueError):
        decode_folder_id("../traversal")


def test_request_fingerprint_changes_when_payload_changes():
    first = parse_run_request(
        {
            "trigger_id": "trigger-1",
            "commit_hash": _FULL_SHA,
            "action": "plan",
            "folder_mode": "all",
            "idempotency_key": "idem-key-12345678",
        }
    )
    second = parse_run_request(
        {
            "trigger_id": "trigger-1",
            "commit_hash": "b" * 40,
            "action": "plan",
            "folder_mode": "all",
            "idempotency_key": "idem-key-12345678",
        }
    )
    assert request_fingerprint(first) != request_fingerprint(second)


def _admin_event(route: str) -> dict[str, object]:
    return {
        "routeKey": route,
        "requestContext": {
            "routeKey": route,
            "authorizer": _API_EVENT["requestContext"]["authorizer"],
        },
    }


def test_api_list_repos_returns_bounded_registration_projection():
    registrations = [
        {
            "repo_name": "org/repo",
            "trigger_ids": ["trigger-1"],
            "require_approval": True,
        }
    ]
    with patch(
        "src.services.api.handler.list_repo_registrations",
        return_value=(registrations, "trigger-1"),
    ) as listing:
        response = api_handler.handler(
            {
                **_admin_event("GET /repos"),
                "queryStringParameters": {"limit": "10"},
            },
            None,
        )
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "repos": registrations,
        "cursor": "trigger-1",
    }
    listing.assert_called_once_with(limit=10, cursor=None)


def test_api_list_accounts_exposes_stored_role_name_not_invented_arns():
    accounts = [
        {
            "alias": "production",
            "account_id": "123456789012",
            "role_name": "openci-tf-executor-remote",
        }
    ]
    with patch(
        "src.services.api.handler.list_account_targets",
        return_value=(accounts, None),
    ):
        response = api_handler.handler(_admin_event("GET /accounts"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"accounts": accounts}
    assert "target_role_arn" not in response["body"]
    assert "hub_role_arn" not in response["body"]


def test_api_list_locks_returns_active_lock_projection():
    locks = [
        {
            "repo_name": "org/repo",
            "folder": "infra/vpc",
            "holder_execution_id": "execution-1",
            "expires_at": 1_700_000_100,
        }
    ]
    with patch(
        "src.services.api.handler.list_active_locks",
        return_value=(locks, None),
    ):
        response = api_handler.handler(_admin_event("GET /locks"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"locks": locks}


def test_active_lock_reader_filters_expired_rows_server_side_and_defensively(monkeypatch):
    class FakeTable:
        def __init__(self):
            self.query_kwargs: dict[str, object] = {}

        def query(self, **kwargs):
            self.query_kwargs = kwargs
            return {
                "Items": [
                    {
                        "pk": "lock",
                        "sk": "org/repo/infra/expired",
                        "holder_execution_id": "old",
                        "expires_at": Decimal(1700000000),
                    },
                    {
                        "pk": "lock",
                        "sk": "org/repo/infra/active",
                        "holder_execution_id": "current",
                        "expires_at": Decimal(1700000001),
                    },
                ]
            }

    table = FakeTable()
    monkeypatch.setenv("LOCKS_TABLE_NAME", "locks")
    monkeypatch.setattr(admin_registry, "dynamo_table", lambda _name: table)
    locks, cursor = admin_registry.list_active_locks(now=1_700_000_000)
    assert cursor is None
    assert locks == [
        {
            "repo_name": "org/repo",
            "folder": "infra/active",
            "holder_execution_id": "current",
            "expires_at": 1_700_000_001,
        }
    ]
    assert table.query_kwargs["FilterExpression"] == (
        "attribute_exists(#expires_at) AND #expires_at > :now"
    )
    assert table.query_kwargs["ExpressionAttributeValues"] == {
        ":pk": "lock",
        ":now": 1_700_000_000,
    }


def test_api_get_gates_reports_latest_pinned_run_observations(monkeypatch):
    monkeypatch.setenv("ENABLE_APPLY", "true")
    folders = [
        {
            "repo_name": "org/repo",
            "folder": "infra/vpc",
            "trigger_id": "trigger-1",
            "run_id": "run-1",
            "source_sha": _FULL_SHA,
            "apply": True,
            "destroy": False,
            "observed_at": 1_700_000_000,
        }
    ]
    with patch(
        "src.services.api.handler.list_folder_gate_projections",
        return_value=(folders, "a" * 64),
    ) as listing:
        response = api_handler.handler(
            {**_admin_event("GET /gates"), "queryStringParameters": {"limit": "10"}},
            None,
        )
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "enable_apply": False,
        "enable_apply_scope": "per-account",
        "folders": folders,
        "folders_source": "latest-run-observation",
        "cursor": "a" * 64,
    }
    listing.assert_called_once_with(limit=10, cursor=None)


@pytest.mark.parametrize("route", ["GET /repos", "GET /accounts", "GET /locks", "GET /gates"])
def test_api_admin_routes_require_admin_read_class_before_io(route: str, monkeypatch):
    monkeypatch.setenv(
        "API_CALLER_POLICY_JSON",
        json.dumps(
            {
                "arn:aws:iam::123456789012:role/test-caller": {
                    "trigger_ids": ["trigger-1"],
                    "actions": ["plan"],
                    "artifact_classes": ["manifest"],
                    "binary_plan": False,
                }
            }
        ),
    )
    forbidden_io = AssertionError("authorization must run before data access")
    with patch("src.services.api.handler.list_repo_registrations", side_effect=forbidden_io), patch(
        "src.services.api.handler.list_account_targets", side_effect=forbidden_io
    ), patch("src.services.api.handler.list_active_locks", side_effect=forbidden_io), patch(
        "src.services.api.handler.list_folder_gate_projections", side_effect=forbidden_io
    ):
        response = api_handler.handler(_admin_event(route), None)
    assert response["statusCode"] == 403


@pytest.mark.parametrize(
    "route",
    [
        "HEAD /repos",
        "POST /repos",
        "GET /repos/",
        "HEAD /accounts",
        "POST /accounts",
        "GET /accounts/",
        "HEAD /locks",
        "POST /locks",
        "GET /locks/",
        "HEAD /gates",
        "POST /gates",
        "GET /gates/",
    ],
)
def test_admin_method_and_path_variants_do_not_reach_io(route: str):
    forbidden_io = AssertionError("unknown admin route variants must not access data")
    with patch("src.services.api.handler.list_repo_registrations", side_effect=forbidden_io), patch(
        "src.services.api.handler.list_account_targets", side_effect=forbidden_io
    ), patch("src.services.api.handler.list_active_locks", side_effect=forbidden_io), patch(
        "src.services.api.handler.list_folder_gate_projections", side_effect=forbidden_io
    ):
        response = api_handler.handler(_admin_event(route), None)
    assert response["statusCode"] == 404


def test_api_list_runs_without_trigger_is_policy_bounded_and_server_filtered(monkeypatch):
    monkeypatch.setenv(
        "API_CALLER_POLICY_JSON",
        json.dumps(
            {
                "arn:aws:iam::123456789012:role/test-caller": {
                    "trigger_ids": ["trigger-2", "trigger-1"],
                    "actions": ["plan", "drift", "report"],
                    "artifact_classes": ["manifest", "text", "json"],
                    "binary_plan": True,
                }
            }
        ),
    )
    runs = [
        {
            "run_id": "run-1",
            "trigger_id": "trigger-1",
            "repo_name": "org/repo",
            "action": "plan",
        }
    ]
    with patch(
        "src.services.api.handler.list_runs_authorized",
        return_value=(runs, "00000000001700000000#run-1"),
    ) as listing:
        response = api_handler.handler(
            {
                **_admin_event("GET /runs"),
                "queryStringParameters": {"repo": "org/repo", "limit": "10"},
            },
            None,
        )
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "runs": runs,
        "cursor": "00000000001700000000#run-1",
    }
    listing.assert_called_once_with(
        ("trigger-1", "trigger-2"),
        actions=frozenset({"plan", "drift", "report"}),
        repo_filter="org/repo",
        limit=10,
        cursor=None,
    )


def test_api_list_runs_omits_empty_pipeline_metadata():
    runs = [
        {
            "run_id": "run-1",
            "trigger_id": "trigger-1",
            "repo_name": "org/repo",
            "action": "plan",
            "pipeline": None,
            "step_count": None,
        },
        {
            "run_id": "run-2",
            "trigger_id": "trigger-1",
            "repo_name": "org/repo",
            "action": "drift",
            "pipeline": "data/primary",
            "step_count": 2,
        },
    ]
    with patch("src.services.api.handler.list_runs_authorized", return_value=(runs, None)):
        response = api_handler.handler(
            {
                **_admin_event("GET /runs"),
                "queryStringParameters": {"trigger_id": "trigger-1"},
            },
            None,
        )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["runs"] == [
        {
            "run_id": "run-1",
            "trigger_id": "trigger-1",
            "repo_name": "org/repo",
            "action": "plan",
        },
        {
            "run_id": "run-2",
            "trigger_id": "trigger-1",
            "repo_name": "org/repo",
            "action": "drift",
            "pipeline": "data/primary",
            "step_count": 2,
        },
    ]


def test_api_list_runs_rejects_unauthorized_trigger_before_io():
    with patch(
        "src.services.api.handler.list_runs_authorized",
        side_effect=AssertionError("authorization must run before registry I/O"),
    ):
        response = api_handler.handler(
            {
                **_admin_event("GET /runs"),
                "queryStringParameters": {"trigger_id": "other-trigger"},
            },
            None,
        )
    assert response["statusCode"] == 403


def test_api_create_run_returns_400_on_invalid_body():
    event = {**_API_EVENT, "body": json.dumps({"action": "plan"})}
    response = api_handler.handler(event, None)
    assert response["statusCode"] == 400


def test_api_create_run_accepts_pipeline_without_folder_mode():
    body = {
        "trigger_id": "trigger-1",
        "commit_hash": _FULL_SHA,
        "action": "plan",
        "pipeline": "data/primary",
        "idempotency_key": "idem-key-12345678",
    }
    with patch("src.services.api.handler.start_run_from_request", return_value=("run-pipeline", True)) as start:
        response = api_handler.handler({**_API_EVENT, "body": json.dumps(body)}, None)
    assert response["statusCode"] == 202
    request = start.call_args.args[0]
    assert request.folder_mode == "pipeline"
    assert request.pipeline == "data/primary"


def test_api_create_run_rejects_pipeline_with_non_pipeline_folder_mode():
    body = {
        "trigger_id": "trigger-1",
        "commit_hash": _FULL_SHA,
        "action": "plan",
        "folder_mode": "explicit",
        "pipeline": "data/primary",
        "idempotency_key": "idem-key-12345678",
    }
    with patch(
        "src.services.api.handler.start_run_from_request",
        side_effect=AssertionError("invalid pipeline payload must not start"),
    ):
        response = api_handler.handler({**_API_EVENT, "body": json.dumps(body)}, None)
    assert response["statusCode"] == 400
    assert "folder_mode must be omitted or pipeline" in response["body"]


def test_justfile_api_create_run_supports_pipeline_alternative():
    justfile = Path("justfile").read_text(encoding="utf-8")
    recipe = justfile.split("api-create-run", 1)[1].split("api-get-run", 1)[0]
    assert 'folder=""' in recipe
    assert 'pipeline=""' in recipe
    assert "set exactly one of folder= or pipeline=" in recipe
    assert 'body["pipeline"] = pipeline' in recipe


def test_api_create_run_denies_unauthorized_principal():
    event = {
        "routeKey": "POST /runs",
        "requestContext": {"authorizer": {"iam": {"userArn": "arn:aws:iam::123456789012:role/other"}}},
        "body": json.dumps(
            {
                "trigger_id": "trigger-1",
                "commit_hash": _FULL_SHA,
                "action": "plan",
                "folder_mode": "all",
                "idempotency_key": "idem-key-12345678",
            }
        ),
    }
    response = api_handler.handler(event, None)
    assert response["statusCode"] == 403


def test_outer_state_machine_propagates_run_id():
    source = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text(encoding="utf-8")
    assert "run_id.$" in source and "$.run_id" in source
    assert "notification_target.$" in source and "$.notification_target" in source


def test_outer_state_machine_concurrency_is_forty():
    source = Path("infra/deploy/modules/openci_tf/step_function.tf").read_text(encoding="utf-8")
    assert "MaxConcurrency = 40" in source


def test_api_gateway_routes_use_aws_iam_authorizer():
    source = Path("infra/deploy/modules/openci_tf/api_gateway.tf").read_text(encoding="utf-8")
    for route in (
        "POST /runs",
        "GET /repos",
        "GET /accounts",
        "GET /locks",
        "GET /gates",
    ):
        block = source.split(f'route_key          = "{route}"', 1)[1].split("}", 1)[0]
        assert 'authorization_type = "AWS_IAM"' in block


def test_execution_artifacts_section_is_copyable():
    section = execution_artifacts_section(
        "run.abc.0",
        "s3://tmp/openci-tf/org/repo/run.abc.0/infra/a/manifest.json",
    )
    assert "Execution ID: `run.abc.0`" in section
    assert "Manifest: `s3://tmp/openci-tf/org/repo/run.abc.0/infra/a/manifest.json`" in section


def test_folder_comment_includes_execution_artifacts_when_provided():
    rendered = folder_comment(
        "infra/a",
        {"succeeded": True, "account_id": "123456789012"},
        {"init.out": "ok", "validate.out": "ok", "tf/plan.out": "Plan: 0 to add"},
        manifest_s3_uri="s3://tmp/openci-tf/org/repo/run/infra/a/manifest.json",
        run_id="run-id",
        repo_name="org/repo",
    )
    assert "### Execution Artifacts" in rendered
    assert rendered.startswith("<details>")
