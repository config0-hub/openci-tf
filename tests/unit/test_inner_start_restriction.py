from pathlib import Path

OPENCI_TF_MAIN = Path("infra/deploy/modules/openci_tf/main.tf").read_text()
OPENCI_TF_IAM = Path("infra/deploy/modules/openci_tf/iam.tf").read_text()


def test_only_outer_role_policy_grants_inner_machine_start_execution():
    policy = OPENCI_TF_IAM
    assert (
        'Action = ["states:StartExecution"], Resource = var.run_folder_state_machine_arn'
        in policy
    )
    assert 'Action = ["states:DescribeExecution", "states:StopExecution"]' in policy
    assert (
        ':execution:${element(split(":", var.run_folder_state_machine_arn), 6)}:*'
        in policy
    )
    assert "Resource = var.run_folder_state_machine_arn" in policy
    assert (
        "run_folder_state_machine_arn"
        not in Path("infra/deploy/modules/run_folder/iam.tf").read_text()
    )


def test_outer_lambda_policy_allows_tmp_metadata_reads_without_plan_prefix_deny():
    assert 'Effect = "Allow"' in OPENCI_TF_IAM
    assert 'Action   = ["s3:GetObject"]' in OPENCI_TF_IAM
    assert 'Resource = "${var.tmp_bucket_arn}/openci-tf/*"' in OPENCI_TF_IAM
    assert "plans/*/plan.tfplan" not in OPENCI_TF_IAM


def test_outer_lambda_policy_grants_bounded_tmp_list_for_render():
    assert 'Action   = "s3:ListBucket"' in OPENCI_TF_IAM
    assert "Resource = var.tmp_bucket_arn" in OPENCI_TF_IAM
    assert '"s3:prefix" = ["openci-tf/*"]' in OPENCI_TF_IAM
    assert (
        "ListBucket on the tmp bucket is required for render list_text_prefix"
        in OPENCI_TF_IAM
    )
    assert OPENCI_TF_IAM.count("s3:ListBucket") == 1
    assert '"${var.tmp_bucket_arn}/openci-tf/*"' in OPENCI_TF_IAM
    assert 'Action   = ["s3:DeleteObject"]' not in OPENCI_TF_IAM


def test_outer_lambda_policy_grants_only_report_all_pointer_tmp_write():
    assert 'Action   = ["s3:PutObject"]' in OPENCI_TF_IAM
    assert (
        'Resource = "${var.tmp_bucket_arn}/openci-tf/*/pr-*/report-all.env"' in OPENCI_TF_IAM
    )
    assert 'Resource = "${var.tmp_bucket_arn}/openci-tf/*"' in OPENCI_TF_IAM
    assert (
        'Action   = ["s3:PutObject"], Resource = "${var.tmp_bucket_arn}/openci-tf/*"'
        not in OPENCI_TF_IAM
    )


def test_outer_lambda_policy_grants_only_report_all_pointer_kms_data_key():
    assert "report_all_pointer_kms_context" in OPENCI_TF_MAIN
    assert (
        '"kms:EncryptionContext:aws:s3:arn" = "${var.tmp_bucket_arn}/openci-tf/*/pr-*/report-all.env"'
        in OPENCI_TF_MAIN
    )
    assert 'Action    = ["kms:GenerateDataKey"]' in OPENCI_TF_IAM
    assert (
        "Condition = merge(local.report_all_pointer_kms_context, local.foundation_kms_via_s3)"
        in OPENCI_TF_IAM
    )
    assert 'Action    = ["kms:GenerateDataKey", "kms:Encrypt"]' not in OPENCI_TF_IAM
