# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""STS AssumeRole helper."""


import boto3


def get_caller_account_id(credentials: dict[str, str] | None = None) -> str:
    """Return the AWS caller account, optionally for freshly assumed credentials."""
    client_args: dict[str, str] = {}
    if credentials is not None:
        client_args = {
            "aws_access_key_id": credentials["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": credentials["AWS_SECRET_ACCESS_KEY"],
            "aws_session_token": credentials["AWS_SESSION_TOKEN"],
        }
    return str(boto3.client("sts", **client_args).get_caller_identity()["Account"])


def assume_role(
    role_arn: str,
    session_name: str = "openci-tf",
    duration_seconds: int = 3600,
    external_id: str | None = None,
    policy_json: str | None = None,
) -> dict[str, str]:
    """Assume an IAM role. Returns temp creds as env var dict."""
    client = boto3.client("sts")
    request = {"RoleArn": role_arn, "RoleSessionName": session_name, "DurationSeconds": duration_seconds}
    if external_id is not None:
        request["ExternalId"] = external_id
    if policy_json is not None:
        request["Policy"] = policy_json
    resp = client.assume_role(**request)
    creds = resp["Credentials"]
    return {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
    }
