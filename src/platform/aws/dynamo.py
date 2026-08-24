# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DynamoDB helpers for repository settings and target-account aliases."""
from __future__ import annotations

import os
from typing import Any

import boto3

from src.core.models import RepoSettings
from src.platform.aws.ssm import get_parameter


def _table(table_name: str):
    resource: Any = boto3.resource("dynamodb")
    return resource.Table(table_name)


def get_repo_settings(
    trigger_id: str,
    table_name: str = os.environ.get("SETTINGS_TABLE_NAME", "openci-tf-settings"),
    *,
    with_webhook_secret: bool = True,
) -> RepoSettings:
    """Fetch repository settings, decrypting the webhook secret only when required."""
    item = _table(table_name).get_item(Key={"pk": "repo", "sk": trigger_id}).get("Item")
    if not item:
        raise ValueError(f"No settings found for trigger_id={trigger_id!r}")
    return RepoSettings(
        trigger_id=item["sk"], repo_name=item["repo_name"], git_url=item["git_url"],
        ssh_url=item.get("ssh_url", ""), ssm_ssh_key=item.get("ssm_ssh_key", ""),
        ssm_openci_tf_github_token=item.get("ssm_openci_tf_github_token", ""),
        s3_bucket_tmp=item.get("s3_bucket_tmp", ""), remote_stateful_bucket=item.get("remote_stateful_bucket", ""),
        secret=(
            get_parameter(item["webhook_secret_ssm"]).strip()
            if with_webhook_secret and item.get("webhook_secret_ssm")
            else ""
        ),
        aws_default_region=item.get("aws_default_region", "us-east-1"), engine_api_url="", engine_webhook_secret="",
        assume_role_arn=item.get("assume_role_arn", ""), jwt_secret_ssm_path=item.get("jwt_secret_ssm_path", ""),
        ssm_infracost_api_key=item.get("ssm_infracost_api_key", ""), upstream_urls=item.get("upstream_urls", {}),
        require_approval=item.get("require_approval", False) is True,
    )


def get_account_alias(alias: str, table_name: str = os.environ.get("SETTINGS_TABLE_NAME", "openci-tf-settings")) -> dict:
    item = _table(table_name).get_item(Key={"pk": "account", "sk": alias}).get("Item")
    if not item:
        raise ValueError(f"Unknown account alias: {alias!r}")
    return item
