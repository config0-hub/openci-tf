# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AWS SSM Parameter Store helpers."""

from __future__ import annotations

import boto3

from src.platform.aws.clone_token import validate_clone_token_path
from src.platform.aws.infracost_key import validate_infracost_key_path


def get_parameter(path: str, with_decryption: bool = True) -> str:
    """Fetch a single SSM parameter value."""
    client = boto3.client("ssm")
    resp = client.get_parameter(Name=path, WithDecryption=with_decryption)
    return resp["Parameter"]["Value"]


def get_github_token(ssm_path: str) -> str:
    """Fetch a validated clone-token parameter from SSM."""
    return get_parameter(validate_clone_token_path(ssm_path)).strip()


def get_infracost_api_key(ssm_path: str) -> str:
    """Fetch a validated Infracost API key from SSM."""
    return get_parameter(validate_infracost_key_path(ssm_path)).strip()
