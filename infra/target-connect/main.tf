# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

data "aws_iam_role" "hub_lambda_exec" {
  count = var.hub_lambda_exec_role_arn == "" ? 1 : 0
  name  = "${var.project_name}-hub-lambda-exec"
}

locals {
  account_id               = data.aws_caller_identity.current.account_id
  hub_account_id           = try(regex("^arn:aws:iam::([0-9]{12}):role/[^:/]+$", local.hub_lambda_exec_role_arn)[0], "")
  state_bucket_arn         = var.state_bucket_arn != "" ? var.state_bucket_arn : "arn:aws:s3:::${var.project_name}-state-${local.account_id}"
  lock_table_arn           = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.project_name}-tf-locks"
  hub_lambda_exec_role_arn = var.hub_lambda_exec_role_arn != "" ? var.hub_lambda_exec_role_arn : data.aws_iam_role.hub_lambda_exec[0].arn
}

module "target_connect" {
  source                   = "../modules/target-connect"
  role_prefix              = var.project_name
  hub_lambda_exec_role_arn = local.hub_lambda_exec_role_arn
  state_bucket_arn         = local.state_bucket_arn
  lock_table_arn           = local.lock_table_arn
  enable_apply             = var.enable_apply
}

module "executor_readonly" {
  source                   = "../modules/executor-readonly"
  role_prefix              = var.project_name
  hub_lambda_exec_role_arn = local.hub_lambda_exec_role_arn
  state_bucket_arn         = local.state_bucket_arn
  lock_table_arn           = local.lock_table_arn
}

resource "terraform_data" "remote_account_only" {
  lifecycle {
    precondition {
      condition     = local.hub_account_id != "" && local.hub_account_id != local.account_id
      error_message = "target-connect is for remote target accounts only; same-account hub readonly is owned by hub-setup deploy (just deploy). See docs/EXECUTOR_ROLES.md."
    }
  }
}
