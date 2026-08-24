# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# ref 4353245 - openci-tf remote executor consistency naming
locals {
  executor_readonly_role_name  = "${var.project_name}-executor-readonly"
  executor_poweruser_role_name = "${var.project_name}-executor-poweruser"
  executor_remote_role_name    = "${var.project_name}-executor-remote"
  executor_local_role_name     = "${var.project_name}-executor-local"
  hub_account_id               = data.aws_caller_identity.current.account_id
  engine_codebuild_project_arn = "arn:aws:codebuild:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:project/${local.engine_codebuild_project_name}"
  prepare_assumable_role_arns = local.mutation_lane ? concat(
    [
      "arn:aws:iam::${local.hub_account_id}:role/${local.executor_poweruser_role_name}",
    ],
    [
      "arn:aws:iam::*:role/${local.executor_poweruser_role_name}",
    ],
    ) : concat(
    [
      "arn:aws:iam::${local.hub_account_id}:role/${local.executor_readonly_role_name}",
      "arn:aws:iam::${local.hub_account_id}:role/${local.executor_local_role_name}",
    ],
    [
      "arn:aws:iam::*:role/${local.executor_readonly_role_name}",
      "arn:aws:iam::*:role/${local.executor_remote_role_name}",
    ],
  )
  installer_cache_objects = [
    for pair in [
      ["terraform", "1.8.5"],
      ["terraform", "1.9.8"],
      ["tofu", "1.8.0"],
      ["tofu", "1.9.0"],
      ["tfsec", "1.28.10"],
      ["infracost", "0.10.39"],
    ] : "${var.package_bucket_arn}/cache/${pair[0]}/${pair[1]}"
  ]
  tmp_kms_context = {
    StringLike = {
      "kms:EncryptionContext:aws:s3:arn" = ["${var.tmp_bucket_arn}/*"]
    }
  }
  package_kms_context = {
    StringLike = {
      "kms:EncryptionContext:aws:s3:arn" = ["${var.package_bucket_arn}/*"]
    }
  }
  done_kms_context = {
    StringLike = {
      "kms:EncryptionContext:aws:s3:arn" = ["${var.done_bucket_arn}/*"]
    }
  }
  foundation_kms_via_s3 = {
    StringEquals = {
      "kms:ViaService" = "s3.${data.aws_region.current.name}.amazonaws.com"
    }
  }
  foundation_kms_via_ssm = {
    StringEquals = {
      "kms:ViaService" = "ssm.${data.aws_region.current.name}.amazonaws.com"
    }
  }
  direct_sops_kms = {
    Null = {
      "kms:ViaService" = "true"
    }
  }
  package_root_zip_objects = ["${var.package_bucket_arn}/*.zip"]
  package_nested_zip_deny  = ["${var.package_bucket_arn}/*/*.zip", "${var.package_bucket_arn}/*/*/*.zip"]
}

resource "aws_iam_role_policy" "prepare" {
  name = "prepare-and-submit"
  role = aws_iam_role.lambda["prepare-and-submit"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["kms:Encrypt", "kms:GenerateDataKey"], Resource = var.kms_key_arn, Condition = local.direct_sops_kms },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.kms_key_arn, Condition = merge(local.package_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Encrypt"], Resource = var.kms_key_arn, Condition = merge(local.package_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.kms_key_arn, Condition = merge(local.tmp_kms_context, local.foundation_kms_via_s3) },
    # /openci-tf/env/* SecureString parameters are encrypted with the foundation KMS key via SSM.
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.kms_key_arn, Condition = local.foundation_kms_via_ssm },
    { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Encrypt"], Resource = var.kms_key_arn, Condition = merge(local.tmp_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = local.prepare_assumable_role_arns },
    # ListBucket on the done bucket is required so absent keys return 404 (not 403);
    # execution IDs are runtime-generated so no tighter prefix condition is feasible.
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${var.done_bucket_arn}/*" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = var.done_bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = local.package_root_zip_objects },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = local.installer_cache_objects },
    { Effect = "Deny", Action = ["s3:GetObject", "s3:PutObject"], Resource = local.package_nested_zip_deny },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${var.tmp_bucket_arn}/openci-tf/*" },
    { Effect = "Allow", Action = "dynamodb:GetItem", Resource = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.project_name}-settings" },
    # Submission acknowledgement is authoritative and must be durable before best-effort PR notification.
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.project_name}-run-registry" },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/openci-tf/clone-token/*" },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/openci-tf/infracost/*" },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/openci-tf/env/*" },
    ], local.prepare_engine_submit_statements, [
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" }
  ]) })
}
resource "aws_iam_role_policy" "poll_done" {
  name = "poll-done"
  role = aws_iam_role.lambda["poll-done"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = "s3:GetObject", Resource = "${var.done_bucket_arn}/*/done" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = var.done_bucket_arn },
    { Effect = "Allow", Action = "kms:Decrypt", Resource = var.kms_key_arn, Condition = merge(local.done_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" }
    ], var.lane != "read" ? [
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:ListBuildsForProject"], Resource = local.engine_codebuild_project_arn },
  ] : []) })
}
resource "aws_iam_role_policy" "collect" {
  name = "collect"
  role = aws_iam_role.lambda["collect"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${var.tmp_bucket_arn}/openci-tf/*" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${var.done_bucket_arn}/*/done" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = var.done_bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = local.package_root_zip_objects },
    { Effect = "Deny", Action = ["s3:GetObject"], Resource = local.package_nested_zip_deny },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = var.tmp_bucket_arn, Condition = { StringLike = { "s3:prefix" = ["openci-tf/*"] } } },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = var.package_bucket_arn, Condition = { StringLike = { "s3:prefix" = ["*.zip"] } } },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.kms_key_arn, Condition = merge(local.tmp_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Encrypt"], Resource = var.kms_key_arn, Condition = merge(local.tmp_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.kms_key_arn, Condition = merge(local.done_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.kms_key_arn, Condition = merge(local.package_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"], Resource = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.project_name}-run-registry" },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" }
  ]) })
}
resource "aws_iam_role_policy" "write_failure_manifest" {
  name = "write-failure-manifest"
  role = aws_iam_role.lambda["write-failure-manifest"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${var.tmp_bucket_arn}/openci-tf/*" },
    { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"], Resource = var.kms_key_arn, Condition = merge(local.tmp_kms_context, local.foundation_kms_via_s3) },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"], Resource = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.project_name}-run-registry" },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" }
  ] })
}
resource "aws_iam_role_policy" "persist_retry_attempt" {
  name = "persist-retry-attempt"
  role = aws_iam_role.lambda["persist-retry-attempt"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:TransactWriteItems"], Resource = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.project_name}-run-registry" },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" }
  ] })
}
resource "aws_iam_role_policy" "sfn" {
  name = "invoke"
  role = aws_iam_role.sfn.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = [for function in aws_lambda_function.this : function.arn] },
    # Step Functions logging to CloudWatch requires log-delivery permissions (resource-level scoping unsupported).
    { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*" }
  ] })
}
