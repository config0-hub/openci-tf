"""Static IAM policy tests for run-folder Lambda roles."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IAM_TF = (_REPO_ROOT / "infra/deploy/modules/run_folder/iam.tf").read_text()
_DONE_OBJECTS_ARN = "${var.done_bucket_arn}/*"
_DONE_BUCKET_ARN = "var.done_bucket_arn"


def _policy_block(role: str) -> str:
    match = re.search(
        rf'resource "aws_iam_role_policy" "{role}" \{{(.*?\n\}})', _IAM_TF, re.DOTALL
    )
    assert match is not None, f"missing policy resource {role}"
    return match.group(1)


def test_mutation_prepare_can_read_run_registry_for_codebuild_progress_link():
    block = _policy_block("prepare")
    assert "${var.project_name}-run-registry" in block
    assert 'Action = "dynamodb:GetItem"' in block


def test_prepare_done_bucket_read_permissions_are_least_privilege():
    block = _policy_block("prepare")
    assert f'Action = ["s3:GetObject"], Resource = "{_DONE_OBJECTS_ARN}"' in block
    assert f'Action = "s3:ListBucket", Resource = {_DONE_BUCKET_ARN}' in block
    assert (
        "ListBucket on the done bucket is required so absent keys return 404 (not 403)"
        in _IAM_TF
    )


def test_prepare_done_bucket_has_no_write_or_delete_on_done_objects():
    block = _policy_block("prepare")
    for action in (
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:GetObjectVersion",
    ):
        assert f'{action}", Resource = "{_DONE_OBJECTS_ARN}"' not in block
        assert f'{action}"], Resource = "{_DONE_OBJECTS_ARN}"' not in block


def test_prepare_package_cache_get_put_allows_pinned_installer_keys():
    block = _policy_block("prepare")
    assert "local.installer_cache_objects" in block
    assert "local.package_root_zip_objects" in block
    assert '["tofu", "1.8.0"]' in _IAM_TF
    assert "${var.tmp_bucket_arn}/openci-tf/*" in block
    assert '"${var.tmp_bucket_arn}/*"' not in block
    assert '"s3:GetObject"], Resource = "${var.tmp_bucket_arn}/*"' not in block


def test_prepare_done_bucket_list_is_bucket_scoped_only():
    block = _policy_block("prepare")
    assert block.count("s3:ListBucket") == 1
    assert f'Action = "s3:ListBucket", Resource = {_DONE_BUCKET_ARN}' in block


def test_prepare_has_least_privilege_ssm_env_access():
    block = _policy_block("prepare")
    assert (
        'Action = "ssm:GetParameter", Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/openci-tf/env/*"'
        in block
    )
    assert "parameter/openci-tf/env/*" in block
    assert block.count("ssm:GetParameter") == 3


def test_prepare_ssm_env_policy_does_not_grant_target_account_ssm():
    assert "parameter/openci-tf/env/*" in _IAM_TF
    assert "sts:AssumeRole" in _policy_block("prepare")
    assert "parameter/openci-tf/env" not in _policy_block("poll_done")


def test_poll_done_done_bucket_permissions_are_unchanged():
    block = _policy_block("poll_done")
    assert (
        'Action = "s3:GetObject", Resource = "${var.done_bucket_arn}/*/done"' in block
    )
    assert f'Action = "s3:ListBucket", Resource = {_DONE_BUCKET_ARN}' in block
    assert "s3:PutObject" not in block
    assert "s3:DeleteObject" not in block
    assert "done_kms_context" in block


def test_collect_role_has_bounded_tmp_manifest_permissions():
    block = _policy_block("collect")
    assert (
        'Action = ["s3:GetObject", "s3:PutObject"], Resource = "${var.tmp_bucket_arn}/openci-tf/*"'
        in block
    )
    assert "s3:CopyObject" not in block
    assert 'Action = "s3:ListBucket", Resource = var.tmp_bucket_arn' in block
    assert '"s3:prefix" = ["openci-tf/*"]' in block
    assert "tmp_kms_context" in block
    assert "kms:GenerateDataKey" in block
    assert "kms:Encrypt" in block
    assert (
        'Action = ["s3:GetObject"], Resource = "${var.done_bucket_arn}/*/done"' in block
    )
    assert (
        'Action = ["s3:GetObject"], Resource = local.package_root_zip_objects' in block
    )
    assert "package_nested_zip_deny" in block
    assert "dynamodb:GetItem" in block
    assert "dynamodb:PutItem" in block
    assert "${var.project_name}-run-registry" in block


def test_collect_role_does_not_probe_tfsec_for_drift_inventory():
    source = (_REPO_ROOT / "src/domain/engine/manifest.py").read_text(encoding="utf-8")
    assert '"drift"' in source
    assert "_artifact_names_for_action" in source


def test_kms_encryption_context_is_scoped_to_foundation_buckets():
    assert "kms:EncryptionContext:aws:s3:arn" in _IAM_TF
    assert "${var.tmp_bucket_arn}/*" in _IAM_TF
    assert "${var.package_bucket_arn}/*" in _IAM_TF
    assert "${var.done_bucket_arn}/*" in _IAM_TF


def test_write_failure_manifest_role_allows_manifest_replay_read():
    block = _policy_block("write_failure_manifest")
    assert (
        'Action = ["s3:PutObject", "s3:GetObject"], Resource = "${var.tmp_bucket_arn}/openci-tf/*"'
        in block
    )
    assert "kms:Decrypt" in block
    assert "dynamodb:GetItem" in block
    assert "dynamodb:TransactWriteItems" in block
    assert "${var.project_name}-run-registry" in block


def test_collect_tmp_list_denies_unapproved_prefixes():
    block = _policy_block("collect")
    assert 'Action = "s3:ListBucket", Resource = var.tmp_bucket_arn' in block
    assert '"s3:prefix" = ["openci-tf/*"]' in block
    assert '"*"' not in block.split("collect")[1].split("write_failure_manifest")[0]


def test_prepare_can_get_and_put_run_artifacts_for_pinned_plan_presign():
    """prepare-and-submit presigns the pinned-plan GET with its own role, so it
    needs GetObject on tmp openci-tf/* (live failure: 'failed to download pinned plan')."""
    start = _IAM_TF.index('aws_iam_role_policy" "prepare')
    end = _IAM_TF.index('resource "aws_iam_role_policy"', start + 10)
    block = _IAM_TF[start:end]
    assert (
        '"s3:GetObject", "s3:PutObject"], Resource = "${var.tmp_bucket_arn}/openci-tf/*"'
        in block
    )
