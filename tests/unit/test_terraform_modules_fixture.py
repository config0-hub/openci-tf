# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static checks for the standalone Terraform test modules fixture."""

from __future__ import annotations

from pathlib import Path

MODULE_ROOT = Path("tests/fixtures/terraform-modules/modules")
EXPECTED_MODULE_RESOURCES = {
    "vpc-basic": {
        'resource "aws_vpc" "this"',
        'resource "aws_internet_gateway" "this"',
        'resource "aws_subnet" "public"',
        'resource "aws_route_table" "public"',
        'resource "aws_route_table_association" "public"',
    },
    "dynamodb-table": {'resource "aws_dynamodb_table" "this"'},
    "sqs-queue": {'resource "aws_sqs_queue" "this"'},
    "cloudwatch-log-group": {'resource "aws_cloudwatch_log_group" "this"'},
    "s3-bucket": {
        'resource "random_id" "suffix"',
        'resource "aws_s3_bucket" "this"',
        'resource "aws_s3_bucket_public_access_block" "this"',
    },
    "sns-topic": {'resource "aws_sns_topic" "this"'},
    "eventbridge-rule": {'resource "aws_cloudwatch_event_rule" "this"'},
}
FORBIDDEN_RESOURCE_PREFIXES = (
    'resource "aws_instance"',
    'resource "aws_iam_',
    'resource "aws_security_group"',
    'resource "aws_nat_gateway"',
    'resource "aws_eip"',
)


def _module_text(module: str) -> str:
    return "\n".join(
        path.read_text() for path in sorted((MODULE_ROOT / module).glob("*.tf"))
    )


def test_expected_low_cost_modules_exist() -> None:
    assert sorted(
        path.name for path in MODULE_ROOT.iterdir() if path.is_dir()
    ) == sorted(EXPECTED_MODULE_RESOURCES)


def test_modules_create_only_their_expected_resource_types() -> None:
    for module, resource_markers in EXPECTED_MODULE_RESOURCES.items():
        text = _module_text(module)
        for marker in resource_markers:
            assert marker in text, f"{module} missing {marker}"
        for forbidden in FORBIDDEN_RESOURCE_PREFIXES:
            assert forbidden not in text, f"{module} contains {forbidden}"


def test_modules_have_provider_versions_variables_and_outputs() -> None:
    for module in EXPECTED_MODULE_RESOURCES:
        module_dir = MODULE_ROOT / module
        for filename in ("versions.tf", "variables.tf", "main.tf", "outputs.tf"):
            assert (module_dir / filename).is_file(), f"{module} missing {filename}"
        assert 'version = "~> 6.0"' in (module_dir / "versions.tf").read_text()
        assert 'variable "tags"' in (module_dir / "variables.tf").read_text()
        assert "output " in (module_dir / "outputs.tf").read_text()


def test_eventbridge_rule_defaults_disabled() -> None:
    text = _module_text("eventbridge-rule")
    assert "default     = false" in text
    assert 'state               = var.enabled ? "ENABLED" : "DISABLED"' in text
