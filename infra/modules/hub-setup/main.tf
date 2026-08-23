# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# hub-lambda-exec role — assumed by openci-tf Lambdas to perform cross-account AssumeRole

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  executor_readonly_role_name  = "${var.role_prefix}-executor-readonly"
  executor_poweruser_role_name = "${var.role_prefix}-executor-poweruser"
  executor_remote_role_name    = "${var.role_prefix}-executor-remote"
  executor_local_role_name     = "${var.role_prefix}-executor-local"
  hub_account_id               = data.aws_caller_identity.current.account_id
  prepare_role_arn             = "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-run-folder-prepare-and-submit"
  target_assumable_role_names = [
    local.executor_readonly_role_name,
    local.executor_poweruser_role_name,
    local.executor_remote_role_name,
  ]
  protected_hub_bucket_arns = [
    "arn:aws:s3:::${var.role_prefix}-tmp-${local.hub_account_id}",
    "arn:aws:s3:::${var.role_prefix}-package-${local.hub_account_id}",
    "arn:aws:s3:::${var.role_prefix}-done-${local.hub_account_id}",
  ]
  protected_hub_role_arns = [
    for name in [
      "${var.role_prefix}-hub-lambda-exec",
      "${var.role_prefix}-executor-readonly",
      "${var.role_prefix}-executor-poweruser",
      "${var.role_prefix}-executor-remote",
      "${var.role_prefix}-executor-local",
      "${var.role_prefix}-lambda-role",
      "${var.role_prefix}-api-lambda-role",
      "${var.role_prefix}-worker",
      "${var.role_prefix}-codebuild",
      "${var.role_prefix}-finalizer",
    ] : "arn:aws:iam::${local.hub_account_id}:role/${name}"
  ]
  protected_hub_resource_arns = concat(
    flatten([
      for arn in local.protected_hub_bucket_arns : [arn, "${arn}/*"]
    ]),
    [
      "arn:aws:dynamodb:${data.aws_region.current.name}:${local.hub_account_id}:table/${var.role_prefix}-locks",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${local.hub_account_id}:table/${var.role_prefix}-locks/index/*",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${local.hub_account_id}:table/${var.role_prefix}-run-registry",
      "arn:aws:dynamodb:${data.aws_region.current.name}:${local.hub_account_id}:table/${var.role_prefix}-run-registry/index/*",
      "arn:aws:lambda:${data.aws_region.current.name}:${local.hub_account_id}:function:${var.role_prefix}-init-job",
      "arn:aws:codebuild:${data.aws_region.current.name}:${local.hub_account_id}:project/${var.role_prefix}-worker",
      "arn:aws:states:${data.aws_region.current.name}:${local.hub_account_id}:stateMachine:${var.role_prefix}-codebuild",
      "arn:aws:states:${data.aws_region.current.name}:${local.hub_account_id}:execution:${var.role_prefix}-codebuild:*",
      "arn:aws:ecr:${data.aws_region.current.name}:${local.hub_account_id}:repository/${var.role_prefix}",
    ],
    local.protected_hub_role_arns,
  )
}

resource "aws_iam_role" "hub_lambda_exec" {
  name = "${var.role_prefix}-hub-lambda-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "hub_lambda_exec" {
  name = "${var.role_prefix}-hub-lambda-exec-policy"
  role = aws_iam_role.hub_lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["sts:AssumeRole"]
      Resource = concat(
        [aws_iam_role.executor_readonly.arn],
        [
          "arn:aws:iam::${local.hub_account_id}:role/${local.executor_poweruser_role_name}",
          "arn:aws:iam::${local.hub_account_id}:role/${local.executor_local_role_name}",
        ],
        flatten([
          for account_id in var.target_account_ids : [
            for role_name in local.target_assumable_role_names :
            "arn:aws:iam::${account_id}:role/${role_name}"
          ]
        ]),
      )
    }]
  })
}

resource "aws_iam_role_policy_attachment" "hub_lambda_exec_basic" {
  role       = aws_iam_role.hub_lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
