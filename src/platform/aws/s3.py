# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S3 helpers for openci-tf."""

from __future__ import annotations

import base64
import json
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

_MANIFEST_CONFLICT_MAX_BYTES = 65_536

_SHA256_HEX = __import__("re").compile(r"^[0-9a-f]{64}$")

_ARTIFACT_CONTENT_TYPES: dict[str, str] = {
    "init.out": "text/plain",
    "validate.out": "text/plain",
    "plan.out": "text/plain",
    "apply.out": "text/plain",
    "plan-show.out": "text/plain",
    "destroy.out": "text/plain",
    "destroy.plan.out": "text/plain",
    "destroy.plan.tfplan": "application/octet-stream",
    "destroy.plan.tfplan.sha256": "text/plain",
    "destroy-plan-metadata.json": "application/json",
    "drift.json": "application/json",
    "tfsec.json": "application/json",
    "infracost.json": "application/json",
    "plan.tfplan": "application/octet-stream",
    "plan.tfplan.sha256": "text/plain",
    "plan-metadata.json": "application/json",
    "manifest.json": "application/json",
}


def _content_type_for_key(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    if name in _ARTIFACT_CONTENT_TYPES:
        return _ARTIFACT_CONTENT_TYPES[name]
    if name.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def _decode_checksum_sha256(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _SHA256_HEX.fullmatch(value):
        return value
    try:
        decoded = base64.b64decode(value)
        if len(decoded) == 32:
            return decoded.hex()
    except (ValueError, TypeError):
        return None
    return None


def head_object(bucket: str, key: str) -> dict[str, Any] | None:
    """Check if an S3 object exists. Returns metadata dict or None."""
    client = boto3.client("s3")
    try:
        resp = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        return {
            "content_length": resp["ContentLength"],
            "last_modified": resp["LastModified"],
            "version_id": resp.get("VersionId"),
            "content_type": resp.get("ContentType", "application/octet-stream"),
            "etag": resp.get("ETag", ""),
            "checksum_sha256": _decode_checksum_sha256(resp.get("ChecksumSHA256")),
        }
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return None
        raise


def _presign_client():
    return boto3.client("s3", config=Config(signature_version="s3v4"))


def presign_get(bucket: str, key: str, expires_in: int) -> str:
    return _presign_client().generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in)


def presign_put(bucket: str, key: str, expires_in: int, *, content_type: str | None = None) -> str:
    ctype = content_type or _content_type_for_key(key)
    return _presign_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": ctype},
        ExpiresIn=expires_in,
    )


def presign_create_put(bucket: str, key: str, expires_in: int, *, content_type: str | None = None) -> str:
    """Presign a create-only S3 PUT; callers must send ``If-None-Match: *``."""
    ctype = content_type or _content_type_for_key(key)
    return _presign_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": ctype, "IfNoneMatch": "*"},
        ExpiresIn=expires_in,
    )


def upload_file(path: str, bucket: str, key: str, *, content_type: str | None = None) -> None:
    """Upload a local file to S3, optionally setting an explicit ContentType."""
    client = boto3.client("s3")
    if content_type:
        client.upload_file(path, bucket, key, ExtraArgs={"ContentType": content_type})
    else:
        client.upload_file(path, bucket, key)


def get_object_bytes(bucket: str, key: str, max_bytes: int) -> bytes | None:
    """Read bounded object bytes. Returns None when the object is absent."""
    client = boto3.client("s3")
    try:
        head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        if head["ContentLength"] > max_bytes:
            raise ValueError(f"object exceeds {max_bytes} bytes: s3://{bucket}/{key}")
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"object exceeds {max_bytes} bytes: s3://{bucket}/{key}")
        return body
    except ClientError as error:
        if error.response["Error"]["Code"] in {"404", "NoSuchKey"}:
            return None
        raise


def put_json_create_only(bucket: str, key: str, payload: dict[str, Any]) -> str:
    """Upload a JSON object only when the key is absent."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    client = boto3.client("s3")
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "PreconditionFailed":
            raise
        existing = get_bounded_json(bucket, key, _MANIFEST_CONFLICT_MAX_BYTES)
        if existing != payload:
            raise ValueError(f"manifest already exists with different content: s3://{bucket}/{key}") from error
        head = head_object(bucket, key)
        return str((head or {}).get("version_id") or "")
    return str(response.get("VersionId") or "")


def get_bounded_json_with_meta(
    bucket: str,
    key: str,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Download bounded JSON from S3 with object metadata. Returns (payload, meta)."""
    client = boto3.client("s3")
    try:
        head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    except ClientError as error:
        if error.response["Error"]["Code"] in {"404", "NoSuchKey"}:
            return None, None
        raise
    content_length = int(head["ContentLength"])
    if content_length > max_bytes:
        raise ValueError(f"JSON object exceeds {max_bytes} bytes: s3://{bucket}/{key}")
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read(max_bytes + 1)
    except ClientError as error:
        if error.response["Error"]["Code"] in {"404", "NoSuchKey"}:
            return None, None
        raise
    if len(body) > max_bytes:
        raise ValueError(f"JSON object exceeds {max_bytes} bytes: s3://{bucket}/{key}")
    if len(body) > content_length:
        raise ValueError(f"JSON object exceeds declared content length: s3://{bucket}/{key}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"malformed JSON object: s3://{bucket}/{key}") from error
    if not isinstance(payload, dict):
        raise TypeError("JSON object must be an object")
    meta = {
        "version_id": response.get("VersionId"),
        "last_modified": response["LastModified"],
        "content_type": response.get("ContentType", "application/json"),
        "content_length": content_length,
    }
    return payload, meta


def get_json_with_meta(bucket: str, key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Download JSON from S3 with object metadata. Returns (payload, meta)."""
    client = boto3.client("s3")
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        payload = json.loads(resp["Body"].read().decode("utf-8"))
        meta = {
            "version_id": resp.get("VersionId"),
            "last_modified": resp["LastModified"],
            "content_type": resp.get("ContentType", "application/json"),
        }
        return payload, meta
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return None, None
        raise


def get_bounded_json(bucket: str, key: str, max_bytes: int) -> dict[str, Any] | None:
    """Download a bounded JSON sidecar. Returns None if absent."""
    client = boto3.client("s3")
    try:
        head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        if head["ContentLength"] > max_bytes:
            raise ValueError(f"JSON sidecar exceeds {max_bytes} bytes: s3://{bucket}/{key}")
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"JSON sidecar exceeds {max_bytes} bytes: s3://{bucket}/{key}")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("JSON sidecar must be an object")
        return payload
    except ClientError as e:
        if e.response["Error"]["Code"] in {"404", "NoSuchKey"}:
            return None
        raise


def copy_object(*, bucket: str, source_key: str, dest_key: str) -> None:
    """Copy one object within the same bucket."""
    client = boto3.client("s3")
    client.copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": source_key},
    )


def get_bounded_text(bucket: str, key: str, max_bytes: int, allowed_content_types: frozenset[str]) -> tuple[str, str] | None:
    """Fetch exactly one bounded text object. Returns (content_type, body) or None."""
    client = boto3.client("s3")
    try:
        head = client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        if head["ContentLength"] > max_bytes:
            raise ValueError(f"artifact exceeds {max_bytes} bytes")
        content_type = head.get("ContentType", "application/octet-stream").split(";", 1)[0]
        if content_type not in allowed_content_types:
            raise ValueError("artifact content type is not allowed")
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError(f"artifact exceeds {max_bytes} bytes")
        return content_type, body.decode("utf-8", errors="replace")
    except ClientError as error:
        if error.response["Error"]["Code"] in {"404", "NoSuchKey"}:
            return None
        raise


def list_text_prefix(bucket: str, prefix: str, max_bytes: int, allowed_content_types: frozenset[str]) -> dict[str, str]:
    """Read approved, bounded text artifacts beneath one execution prefix.

    The caller receives a bounded rejection marker rather than an untrusted body.
    """
    client = boto3.client("s3")
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    artifacts: dict[str, str] = {}
    for item in response.get("Contents", []):
        key, size = item["Key"], item["Size"]
        name = key.removeprefix(prefix)
        if size > max_bytes:
            artifacts[name] = "[artifact rejected: exceeds size limit]"
            continue
        response = client.get_object(Bucket=bucket, Key=key)
        content_type = response.get("ContentType", "application/octet-stream").split(";", 1)[0]
        if content_type not in allowed_content_types:
            artifacts[name] = "[artifact rejected: unsupported content type]"
            continue
        body = response["Body"].read(max_bytes + 1)
        if len(body) > max_bytes:
            artifacts[name] = "[artifact rejected: exceeds size limit]"
            continue
        artifacts[name] = body.decode("utf-8", errors="replace")
    return artifacts
