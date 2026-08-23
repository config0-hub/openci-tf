# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Terraform contract tests for engine compatibility policies."""

import re
from pathlib import Path


SOURCE = Path("infra/deploy/engine_compat.tf").read_text()
DEPLOY_MAIN = Path("infra/deploy/main.tf").read_text()
RUN_FOLDER_IAM = Path("infra/deploy/modules/run_folder/iam.tf").read_text()
RUN_FOLDER_LAMBDAS = Path("infra/deploy/modules/run_folder/lambdas.tf").read_text()
OPENCI_TF_IAM = Path("infra/deploy/modules/openci_tf/iam.tf").read_text()


def _resource_block(resource_name: str) -> str:
    pattern = rf'resource "aws_iam_role_policy" "{resource_name}" \{{(.*?\n\}})'
    match = re.search(pattern, SOURCE, re.S)
    assert match, f"missing policy resource {resource_name}"
    return match.group(1)


def test_mutation_lanes_use_engine_worker_project_not_state_machine_name():
    assert 'engine_codebuild_project_name      = "${var.project_name}-worker"' in DEPLOY_MAIN
    assert "data.aws_codebuild_project.engine_worker.name" not in DEPLOY_MAIN
    assert "project/${local.engine_codebuild_project_name}" in RUN_FOLDER_IAM
    assert (
        "ENGINE_CODEBUILD_PROJECT_NAME      = local.engine_codebuild_project_name"
        in RUN_FOLDER_LAMBDAS
    )
    assert (
        'ENGINE_CODEBUILD_PROJECT_NAME = "${var.project_name}-codebuild"'
        not in RUN_FOLDER_LAMBDAS
    )
    assert "project/${var.project_name}-worker" in OPENCI_TF_IAM
    assert "codebuild:BatchGetBuilds" in OPENCI_TF_IAM
    assert "codebuild:ListBuildsForProject" in OPENCI_TF_IAM


def test_engine_codebuild_gets_foundation_kms_decrypt_and_data_key():
    block = _resource_block("engine_codebuild_foundation_kms")
    assert 'Action   = ["kms:Decrypt", "kms:GenerateDataKey"]' in block
    assert "Resource = data.aws_kms_alias.foundation.target_key_arn" in block


def test_engine_finalizer_gets_only_foundation_done_write_and_kms():
    block = _resource_block("engine_finalizer_foundation_done")
    assert 'Action   = ["s3:PutObject"]' in block
    assert 'Resource = "${data.aws_s3_bucket.done.arn}/*"' in block
    assert 'Action   = ["kms:GenerateDataKey"]' in block
    assert "kms:Decrypt" not in block


def test_prepare_role_reads_only_reserved_ssm_namespaces():
    assert "parameter/openci-tf/clone-token/*" in RUN_FOLDER_IAM
    assert "parameter/openci-tf/infracost/*" in RUN_FOLDER_IAM
    assert "parameter/openci-tf/install/*" not in RUN_FOLDER_IAM
    assert "parameter/${var.project_name}/*" not in RUN_FOLDER_IAM


def test_retry_task_passes_the_complete_pinned_clone_envelope():
    from tests.helpers.rendered_run_folder_asl import load_rendered_run_folder_definition

    for lane in ("read", "apply", "destroy"):
        task = load_rendered_run_folder_definition(lane)["States"][
            "BookkeepCredentialRetry"
        ]
        assert task["Parameters"]["event.$"] == "$"
        assert task["ResultPath"] == "$"
        assert task["Next"] == "PrepareAndSubmit"
