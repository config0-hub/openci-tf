#!/usr/bin/env python3
"""Mechanical contract tests for the multi-region openci-tf tracer sample."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TERRAFORM_ROOT = REPO_ROOT / "terraform"
EXPECTED_REGIONS = {
    "eu-west-1": "10.40.0.0/16",
    "ap-northeast-1": "10.41.0.0/16",
}
TARGET_REGIONS = {
    "target-eu-west-1": ("eu-west-1", "10.42.0.0/16"),
    "target-us-east-1": ("us-east-1", "10.43.0.0/16"),
}
EXPECTED_ROOTS = [
    "ap-northeast-1/01-vpc",
    "ap-northeast-1/02-ec2",
    "eu-west-1/01-vpc",
    "eu-west-1/02-ec2",
    "target-eu-west-1/01-vpc",
    "target-us-east-1/01-vpc",
]
ALLOWED_ACCOUNT = "111111111111"
TARGET_ACCOUNT = "222222222222"
MAIN_ALIAS = "primary"
TARGET_ALIAS = "remote"
TARGET_STATE_BUCKET = "openci-tf-state-222222222222"
REPO_SLUG = "<REPO_ORG>/<REPO_NAME>"
STATE_BUCKET = "openci-tf-state-111111111111"
BACKEND_REGION = "us-east-1"
OPENCI_TF_SAFE_ACTIONS = frozenset({"plan", "drift", "validate", "report"})
OPENCI_TF_FOLDER_CONFIG_KEYS = frozenset(
    {
        "version",
        "account_alias",
        "execution_target",
        "tf_runtime",
        "timeout",
        "extra_flags",
    }
)
BACKEND_REQUIRED_FIELDS = {
    "bucket": STATE_BUCKET,
    "region": BACKEND_REGION,
    "encrypt": "true",
}
VPC_STATE_SUFFIX = "/01-vpc.tfstate"
WORKLOAD_STATE_SUFFIX = ".tfstate"  # shared region-state key used by 02-ec2


def aws_free_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AWS_", "TF_VAR_aws"))
    }


def read_root_files(root: str) -> str:
    root_dir = TERRAFORM_ROOT / root
    chunks = []
    for path in sorted(root_dir.glob("*.tf")):
        chunks.append(path.read_text())
    return "\n".join(chunks)


def all_terraform_text() -> str:
    return "\n".join(path.read_text() for path in sorted(TERRAFORM_ROOT.rglob("*.tf")))


def parse_backend_block(root: str) -> dict[str, str]:
    """Extract backend settings from versions.tf without contacting AWS."""
    versions = (TERRAFORM_ROOT / root / "versions.tf").read_text()
    match = re.search(r'backend\s+"s3"\s*\{([^}]*)\}', versions, re.DOTALL)
    if not match:
        raise AssertionError(f"missing backend block in {root}")
    block = match.group(1)
    fields: dict[str, str] = {}
    for line in block.splitlines():
        field_match = re.match(r'\s*(\w+)\s*=\s*"([^"]*)"', line)
        if field_match:
            fields[field_match.group(1)] = field_match.group(2)
        bool_match = re.match(r"\s*(\w+)\s*=\s*(true|false)", line)
        if bool_match:
            fields[bool_match.group(1)] = bool_match.group(2)
    return fields


def parse_yaml_mapping_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_]+):", line)
        if match:
            keys.add(match.group(1))
    return keys


class TerraformContractTests(unittest.TestCase):
    def test_exactly_six_reportable_roots(self) -> None:
        roots = sorted(
            f"{region.name}/{layer.name}"
            for region in TERRAFORM_ROOT.iterdir()
            if region.is_dir()
            for layer in region.iterdir()
            if layer.is_dir() and (layer / ".openci_tf" / "config.yaml").exists()
        )
        self.assertEqual(roots, sorted(EXPECTED_ROOTS))

    def test_vpc_cidrs_per_region(self) -> None:
        for region, cidr in EXPECTED_REGIONS.items():
            content = read_root_files(f"{region}/01-vpc")
            self.assertIn(f'aws_region      = "{region}"', content)
            self.assertIn(f'vpc_cidr        = "{cidr}"', content)

    def test_no_nat_resources(self) -> None:
        content = all_terraform_text()
        self.assertNotIn("aws_nat_gateway", content)
        self.assertNotIn("aws_eip", content)

    def test_one_public_subnet_per_vpc(self) -> None:
        for region in EXPECTED_REGIONS:
            content = read_root_files(f"{region}/01-vpc")
            self.assertEqual(content.count('resource "aws_subnet" "public"'), 1, region)
            self.assertNotIn(
                "for_each = toset(data.aws_availability_zones.available.names)", content
            )
            self.assertIn(
                "selected_availability_zone = sort(data.aws_availability_zones.available.names)[0]",
                content,
            )
            self.assertIn(
                "availability_zone       = local.selected_availability_zone", content
            )

    def test_one_t3_nano_per_workload_root(self) -> None:
        for region in EXPECTED_REGIONS:
            content = read_root_files(f"{region}/02-ec2")
            instances = re.findall(r'resource\s+"aws_instance"\s+"probe"', content)
            nano = re.findall(r'instance_type\s*=\s*"t3\.nano"', content)
            self.assertEqual(len(instances), 1, region)
            self.assertEqual(len(nano), 1, region)
            self.assertIn(
                "subnet_id                   = data.terraform_remote_state.vpc.outputs.public_subnet_id",
                content,
            )

    def test_workload_roots_read_vpc_remote_state(self) -> None:
        for region in EXPECTED_REGIONS:
            content = read_root_files(f"{region}/02-ec2")
            self.assertIn('data "terraform_remote_state" "vpc"', content)
            self.assertIn(f"terraform/{region}/01-vpc.tfstate", content)
            self.assertIn(
                "vpc_id      = data.terraform_remote_state.vpc.outputs.vpc_id", content
            )

    def test_zero_security_group_ingress(self) -> None:
        for region in EXPECTED_REGIONS:
            content = read_root_files(f"{region}/02-ec2")
            self.assertNotIn("ingress {", content)
            self.assertNotIn('type              = "ingress"', content)
            self.assertNotIn("aws_security_group_rule", content)

    def test_encrypted_gp3_root_volume(self) -> None:
        content = all_terraform_text()
        self.assertEqual(content.count('volume_type           = "gp3"'), 2)
        self.assertEqual(content.count("encrypted             = true"), 2)

    def test_imdsv2_required(self) -> None:
        content = all_terraform_text()
        self.assertEqual(content.count('http_tokens                 = "required"'), 2)

    def test_messaging_wiring_per_region(self) -> None:
        for region in EXPECTED_REGIONS:
            content = read_root_files(f"{region}/02-ec2")
            self.assertIn('resource "aws_sns_topic" "events"', content)
            self.assertIn('resource "aws_sqs_queue" "main"', content)
            self.assertIn('resource "aws_sqs_queue" "dlq"', content)
            self.assertIn('resource "aws_sns_topic_subscription" "queue"', content)
            self.assertIn('resource "aws_sqs_queue_policy" "main"', content)
            self.assertIn("deadLetterTargetArn", content)
            self.assertIn("sqs_managed_sse_enabled", content)
            self.assertIn("aws:SourceArn", content)
            self.assertIn('kms_master_key_id = "alias/aws/sns"', content)

    def test_dynamodb_pay_per_request_pitr_off(self) -> None:
        for region in EXPECTED_REGIONS:
            content = read_root_files(f"{region}/02-ec2")
            self.assertIn('billing_mode = "PAY_PER_REQUEST"', content)
            self.assertIn('hash_key     = "pk"', content)
            self.assertIn('range_key    = "sk"', content)
            self.assertIn("point_in_time_recovery", content)
            self.assertIn("enabled = false", content)

    def test_account_guard_present(self) -> None:
        content = all_terraform_text()
        self.assertEqual(content.count(f'allowed_account = "{ALLOWED_ACCOUNT}"'), 4)
        self.assertEqual(content.count(f'allowed_account = "{TARGET_ACCOUNT}"'), 2)
        self.assertEqual(content.count("allowed_account_ids"), 6)
        self.assertEqual(content.count('resource "terraform_data" "account_guard"'), 6)

    def test_account_guard_wired_to_all_resource_families(self) -> None:
        for region in EXPECTED_REGIONS:
            vpc_content = read_root_files(f"{region}/01-vpc")
            workload_content = read_root_files(f"{region}/02-ec2")
            vpc_section = vpc_content.split('resource "aws_vpc" "main"', 1)[1].split(
                "resource ", 1
            )[0]
            self.assertIn("depends_on = [terraform_data.account_guard]", vpc_section)
            for marker in (
                'resource "aws_security_group" "probe"',
                'resource "aws_iam_role" "probe"',
                'resource "aws_sqs_queue" "dlq"',
                'resource "aws_dynamodb_table" "tracer"',
            ):
                section = workload_content.split(marker, 1)[1].split("resource ", 1)[0]
                self.assertIn(
                    "depends_on = [terraform_data.account_guard]", section, marker
                )

    def test_al2023_ssm_parameter(self) -> None:
        content = all_terraform_text()
        self.assertEqual(
            content.count(
                'name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"'
            ),
            2,
        )

    def test_only_ssm_managed_policy_attachment(self) -> None:
        content = all_terraform_text()
        policy_attachments = re.findall(
            r'resource\s+"aws_iam_role_policy_attachment"\s+"(\w+)"', content
        )
        self.assertEqual(policy_attachments, ["ssm_core", "ssm_core"])
        self.assertNotIn('resource "aws_iam_role_policy"', content)
        self.assertNotIn('"Action"    = "*"', content)

    def test_disposable_tags_documented(self) -> None:
        content = all_terraform_text()
        for tag in (
            "ManagedBy",
            "Project",
            "Environment",
            "Region",
            "ExpiresOn",
            "Owner",
            "Disposable",
        ):
            self.assertIn(tag, content)
        self.assertIn('lifecycle_start_date = "2026-01-01"', content)
        self.assertIn('ExpiresOn   = "2026-01-08"', content)

    def test_backend_block_complete_per_root(self) -> None:
        for root in EXPECTED_ROOTS:
            fields = parse_backend_block(root)
            if root.startswith("target-"):
                for key in ("region", "encrypt"):
                    self.assertEqual(
                        fields.get(key),
                        BACKEND_REQUIRED_FIELDS[key],
                        f"{root} backend {key}",
                    )
                self.assertEqual(fields.get("bucket"), TARGET_STATE_BUCKET, root)
                expected_key = f"targets/{REPO_SLUG}/terraform/{root}.tfstate"
            else:
                for key, expected in BACKEND_REQUIRED_FIELDS.items():
                    self.assertEqual(fields.get(key), expected, f"{root} backend {key}")
                if root.endswith("/01-vpc"):
                    expected_key = f"targets/{REPO_SLUG}/terraform/{root}.tfstate"
                else:
                    region = root.split("/")[0]
                    expected_key = f"targets/{REPO_SLUG}/terraform/{region}.tfstate"
            self.assertEqual(fields.get("key"), expected_key)

    def test_target_vpc_roots_only_in_expected_regions(self) -> None:
        for folder, (region, cidr) in TARGET_REGIONS.items():
            content = read_root_files(f"{folder}/01-vpc")
            self.assertIn(f'aws_region      = "{region}"', content)
            self.assertIn(f'vpc_cidr        = "{cidr}"', content)
            self.assertNotIn("ap-northeast-1", content)
        self.assertNotIn("target-ap-northeast", all_terraform_text())

    def test_backend_schema_allows_plain_init_without_aws(self) -> None:
        """Check offline init has bucket/key/region without contacting AWS."""
        for root in EXPECTED_ROOTS:
            fields = parse_backend_block(root)
            missing = [
                name for name in ("bucket", "key", "region") if not fields.get(name)
            ]
            self.assertEqual(missing, [], root)
            with tempfile.TemporaryDirectory() as tf_data_dir:
                init = subprocess.run(
                    [
                        "tofu",
                        f"-chdir={TERRAFORM_ROOT / root}",
                        "init",
                        "-backend=false",
                        "-input=false",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={**aws_free_env(), "TF_DATA_DIR": tf_data_dir},
                )
            self.assertEqual(init.returncode, 0, init.stderr)


class OpenciTfConfigTests(unittest.TestCase):
    def test_folder_configs_exist_and_safe(self) -> None:
        configs = sorted(TERRAFORM_ROOT.glob("*/*/.openci_tf/config.yaml"))
        self.assertEqual(len(configs), 6)
        for path in configs:
            text = path.read_text()
            keys = parse_yaml_mapping_keys(text)
            self.assertTrue(keys.issubset(OPENCI_TF_FOLDER_CONFIG_KEYS), keys)
            rel = path.relative_to(TERRAFORM_ROOT).as_posix()
            if rel.startswith("target-"):
                self.assertIn(f"account_alias: {TARGET_ALIAS}", text)
            else:
                self.assertIn(f"account_alias: {MAIN_ALIAS}", text)
            self.assertIn("execution_target: lambda", text)
            lowered = text.lower()
            self.assertNotIn("apply:", lowered)
            self.assertNotIn("destroy:", lowered)

    def test_global_config_has_no_apply_destroy(self) -> None:
        text = (REPO_ROOT / ".openci_tf" / "config.yaml").read_text().lower()
        self.assertNotIn("apply:", text)
        self.assertNotIn("destroy:", text)

    def test_openci_tf_safe_lane_matches_outer_state_contract(self) -> None:
        # outer_state.resolve_outer_state returns {} for apply/destroy; RESOLVERS omits them.
        disabled = frozenset({"apply", "destroy"})
        self.assertTrue(OPENCI_TF_SAFE_ACTIONS.isdisjoint(disabled))
        self.assertEqual(
            OPENCI_TF_SAFE_ACTIONS, frozenset({"plan", "drift", "validate", "report"})
        )
        for path in sorted(TERRAFORM_ROOT.glob("*/*/.openci_tf/config.yaml")):
            text = path.read_text()
            for verb in disabled:
                self.assertNotIn(f"{verb}:", text.lower())
            self.assertNotIn("extra_flags:", text)


class RepositoryDocsTests(unittest.TestCase):
    def test_readme_covers_operational_boundary(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text()
        for phrase in (
            "openci-tf",
            "plan",
            "apply",
            "destroy",
            "same-account",
            "remote-target",
            "01-vpc",
            "02-ec2",
            "Preconditions",
            "GitHub webhook",
            "tf plan all",
        ):
            self.assertIn(phrase, readme)

    def test_justfile_requires_confirmations_without_backend_injection(self) -> None:
        justfile = (REPO_ROOT / "justfile").read_text()
        self.assertIn(ALLOWED_ACCOUNT, justfile)
        self.assertIn(TARGET_ACCOUNT, justfile)
        self.assertIn(REPO_SLUG, justfile)
        self.assertIn("confirm-primary-account", justfile)
        self.assertIn("confirm-target-account", justfile)
        self.assertIn("confirm-destroy", justfile)
        self.assertNotIn("-backend-config", justfile)
        self.assertNotIn("backend-config", justfile)


class OfflineValidationTests(unittest.TestCase):
    def test_validate_offline_uses_isolated_tf_data_dir(self) -> None:
        justfile = (REPO_ROOT / "justfile").read_text()
        validate_offline = justfile.split("validate-offline root:", 1)[1].split(
            "\n\n", 1
        )[0]
        self.assertIn("mktemp -d", validate_offline)
        self.assertIn('TF_DATA_DIR="$tf_data_dir"', validate_offline)
        self.assertIn("trap cleanup EXIT", validate_offline)
        self.assertIn("init -backend=false", validate_offline)
        self.assertIn("validate", validate_offline)

    def test_validate_offline_runs_without_aws_credentials(self) -> None:
        env = aws_free_env()
        for root in EXPECTED_ROOTS:
            result = subprocess.run(
                ["just", "validate-offline", root],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(
                result.returncode, 0, f"{root}: {result.stderr}\n{result.stdout}"
            )


if __name__ == "__main__":
    unittest.main()
