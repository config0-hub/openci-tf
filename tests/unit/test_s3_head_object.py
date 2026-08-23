"""S3 head_object semantics for done-marker baseline reads."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from src.platform.aws import s3


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "HeadObject")


def test_head_object_returns_metadata_for_existing_key(monkeypatch):
    client = Mock()
    client.head_object.return_value = {
        "ContentLength": 12,
        "LastModified": datetime(2026, 8, 7, tzinfo=timezone.utc),
        "VersionId": "baseline-version",
    }
    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: client)

    assert s3.head_object("done-bucket", "run/done") == {
        "content_length": 12,
        "last_modified": datetime(2026, 8, 7, tzinfo=timezone.utc),
        "version_id": "baseline-version",
        "content_type": "application/octet-stream",
        "etag": "",
        "checksum_sha256": None,
    }
    client.head_object.assert_called_once_with(Bucket="done-bucket", Key="run/done", ChecksumMode="ENABLED")


def test_head_object_missing_key_returns_no_baseline(monkeypatch):
    client = Mock()
    client.head_object.side_effect = _client_error("404")
    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: client)

    assert s3.head_object("done-bucket", "run/done") is None


def test_head_object_access_denied_remains_fail_loud(monkeypatch):
    client = Mock()
    client.head_object.side_effect = _client_error("403")
    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: client)

    with pytest.raises(ClientError) as exc:
        s3.head_object("done-bucket", "run/done")
    assert exc.value.response["Error"]["Code"] == "403"


def test_put_json_create_only_is_idempotent_for_matching_payload(monkeypatch):
    client = Mock()
    client.put_object.return_value = {"VersionId": "v1"}
    monkeypatch.setattr(s3.boto3, "client", lambda *_args, **_kwargs: client)

    payload = {"version": 1, "execution_id": "run"}
    assert s3.put_json_create_only("tmp", "run/manifest.json", payload) == "v1"
    client.put_object.assert_called_once()
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
