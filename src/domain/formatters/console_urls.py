"""Pure AWS console URL builders for Step Functions and CodeBuild."""

from __future__ import annotations

import re
from urllib.parse import quote, urlparse

from src.core.aws_ids import is_valid_codebuild_build_id

_ARN_REGION = re.compile(r"^arn:aws:[^:]+:([^:]+):")
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ACCOUNT_ID = re.compile(r"^\d{12}$")
_CONSOLE_ROLE_NAME = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$")


def _validate_region(region: str) -> str:
    if not isinstance(region, str) or not region:
        raise ValueError("region is required")
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", region):
        raise ValueError(f"invalid AWS region: {region}")
    return region


def _validate_execution_arn(execution_arn: str) -> str:
    if not isinstance(execution_arn, str) or not execution_arn.startswith(
        "arn:aws:states:"
    ):
        raise ValueError("execution_arn must be a Step Functions execution ARN")
    return execution_arn


def region_from_arn(arn: str) -> str:
    match = _ARN_REGION.match(arn)
    if not match:
        raise ValueError(f"cannot parse region from ARN: {arn}")
    return _validate_region(match.group(1))


def step_functions_execution_url(
    execution_arn: str, *, region: str | None = None
) -> str:
    """Return a console URL for one Step Functions execution."""
    arn = _validate_execution_arn(execution_arn)
    resolved_region = _validate_region(region or region_from_arn(arn))
    encoded = quote(arn, safe=":/")
    return (
        f"https://console.aws.amazon.com/states/home?region={resolved_region}"
        f"#/executions/details/{encoded}"
    )


def _normalize_identity_center_start_url(start_url: str) -> str:
    if not isinstance(start_url, str) or not start_url.strip():
        raise ValueError("identity center start URL is required")
    cleaned = start_url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("identity center start URL must be an https URL")
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0]
    return cleaned.rstrip("/")


def identity_center_console_url(
    destination_url: str,
    *,
    start_url: str,
    account_id: str,
    role_name: str,
) -> str:
    """Return an IAM Identity Center shortcut URL for a console destination."""
    parsed_destination = urlparse(destination_url)
    if parsed_destination.scheme != "https" or not parsed_destination.netloc:
        raise ValueError("destination_url must be an https URL")
    if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("account_id must be 12 digits")
    if not isinstance(role_name, str) or not _CONSOLE_ROLE_NAME.fullmatch(role_name):
        raise ValueError("role_name must be a valid IAM Identity Center role name")
    normalized_start = _normalize_identity_center_start_url(start_url)
    return (
        f"{normalized_start}/#/console?account_id={account_id}"
        f"&role_name={quote(role_name, safe='')}"
        f"&destination={quote(destination_url, safe='')}"
    )


def codebuild_build_url(
    project_name: str,
    build_id: str,
    *,
    region: str,
    account_id: str | None = None,
    identity_center_start_url: str | None = None,
    identity_center_role_name: str | None = None,
) -> str:
    """Return a console URL for one CodeBuild build."""
    resolved_region = _validate_region(region)
    if not isinstance(project_name, str) or not _PROJECT_NAME.fullmatch(project_name):
        raise ValueError("project_name must be a valid CodeBuild project name")
    if not is_valid_codebuild_build_id(build_id):
        raise ValueError("build_id must be a valid CodeBuild build identifier")
    encoded_project = quote(project_name, safe="")
    encoded_build = quote(build_id, safe=":")
    destination = (
        f"https://{resolved_region}.console.aws.amazon.com/codesuite/codebuild/"
        f"{resolved_region}/projects/{encoded_project}/build/{encoded_build}/?region={resolved_region}"
    )
    if account_id and identity_center_start_url and identity_center_role_name:
        return identity_center_console_url(
            destination,
            start_url=identity_center_start_url,
            account_id=account_id,
            role_name=identity_center_role_name,
        )
    return destination
