# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# executor-readonly role — read lane only; never receives PowerUserAccess.

locals {
  executor_readonly_role_name            = "${var.role_prefix}-executor-readonly"
  executor_readonly_boundary_policy_name = "${local.executor_readonly_role_name}-permissions-boundary"
}

data "aws_caller_identity" "current" {}

locals {
  hub_account_id                    = try(regex("^arn:aws:iam::([0-9]{12}):role/[^:/]+$", var.hub_lambda_exec_role_arn)[0], "")
  expected_hub_lambda_exec_role_arn = "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-hub-lambda-exec"
  target_account_id                 = data.aws_caller_identity.current.account_id
  target_iam_role_arns              = "arn:aws:iam::${local.target_account_id}:role/*"
  target_iam_instance_profile_arns  = "arn:aws:iam::${local.target_account_id}:instance-profile/*"
  external_id                       = "openci-tf-${substr(sha256("openci-tf:${local.hub_account_id}:${local.target_account_id}"), 0, 16)}"
  terraform_plan_time_iam_read_scoped_actions = [
    "iam:GetRole",
    "iam:GetRolePolicy",
    "iam:ListRolePolicies",
    "iam:ListAttachedRolePolicies",
    "iam:GetInstanceProfile",
    "iam:ListInstanceProfilesForRole",
  ]
  terraform_plan_time_iam_read_scoped_resources = [
    local.target_iam_role_arns,
    local.target_iam_instance_profile_arns,
  ]
  # iam:ListRoles and iam:SimulatePrincipalPolicy cannot be account-scoped; "*" is required.
  terraform_plan_time_iam_read_wildcard_actions = [
    "iam:ListRoles",
    "iam:SimulatePrincipalPolicy",
  ]
  hub_role_prefix_arns = [
    "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-hub-lambda-exec",
    "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-run-folder-prepare-and-submit",
  ]
}

resource "aws_iam_policy" "executor_readonly_permissions_boundary" {
  name        = local.executor_readonly_boundary_policy_name
  description = "Permissions boundary ceiling for executor-readonly; effective grants are the intersection with ReadOnlyAccess and inline policy."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BoundaryBroadWorkloadAllow"
        Effect = "Allow"
        Action = "*"
        NotResource = [
          var.state_bucket_arn,
          "${var.state_bucket_arn}/*",
          var.lock_table_arn,
          "${var.lock_table_arn}/index/*",
          "${var.state_bucket_arn}/source/*",
          "${var.state_bucket_arn}/engine/*",
          "${var.state_bucket_arn}/bootstrap/*",
          "${var.state_bucket_arn}/foundation/*",
          "${var.state_bucket_arn}/deploy/*",
          "${var.state_bucket_arn}/target-connect/*",
          "${var.state_bucket_arn}/target-connect-readonly/*",
          "${var.state_bucket_arn}/target-connect-poweruser/*",
          "${var.state_bucket_arn}/engine-00-bootstrap/*",
          "${var.state_bucket_arn}/engine-02-deploy/*",
        ]
      },
      {
        Sid      = "BoundaryTerraformTargetStateReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${var.state_bucket_arn}/targets/*"
      },
      {
        Sid       = "BoundaryTerraformTargetStateList"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = var.state_bucket_arn
        Condition = { StringLike = { "s3:prefix" = "targets/*" } }
      },
      {
        Sid       = "BoundaryTerraformTargetLockReadWrite"
        Effect    = "Allow"
        Action    = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"]
        Resource  = var.lock_table_arn
        Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["*/targets/*"] } }
      },
      {
        Sid      = "BoundaryTerraformTargetLockDescribe"
        Effect   = "Allow"
        Action   = ["dynamodb:DescribeTable"]
        Resource = var.lock_table_arn
      },
      {
        Sid      = "BoundaryTerraformPlanTimeIamReadsScoped"
        Effect   = "Allow"
        Action   = local.terraform_plan_time_iam_read_scoped_actions
        Resource = local.terraform_plan_time_iam_read_scoped_resources
      },
      {
        Sid      = "BoundaryTerraformPlanTimeIamReadsWildcard"
        Effect   = "Allow"
        Action   = local.terraform_plan_time_iam_read_wildcard_actions
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role" "executor_readonly" {
  name                 = local.executor_readonly_role_name
  permissions_boundary = aws_iam_policy.executor_readonly_permissions_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      Principal = {
        AWS = "arn:aws:iam::${local.hub_account_id}:root"
      }
      Condition = {
        StringEquals = {
          "sts:ExternalId"   = local.external_id
          "aws:PrincipalArn" = local.hub_role_prefix_arns
        }
      }
    }]
  })

  lifecycle {
    precondition {
      condition     = var.hub_lambda_exec_role_arn == local.expected_hub_lambda_exec_role_arn && can(regex("^\\d{12}$", local.target_account_id))
      error_message = "hub_lambda_exec_role_arn must be exactly arn:aws:iam::<12-digit-hub-account-id>:role/${var.role_prefix}-hub-lambda-exec (standard aws partition, no path, suffix, or extra ARN segments); target AWS account id must be 12 decimal digits"
    }
  }
}

resource "aws_iam_role_policy" "executor_readonly" {
  name = "${local.executor_readonly_role_name}-policy"
  role = aws_iam_role.executor_readonly.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "TerraformTargetStateReadWrite"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${var.state_bucket_arn}/targets/*"
      },
      {
        Sid       = "TerraformTargetStateList"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = var.state_bucket_arn
        Condition = { StringLike = { "s3:prefix" = "targets/*" } }
      },
      {
        Sid       = "TerraformTargetLockReadWrite"
        Effect    = "Allow"
        Action    = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:DescribeTable"]
        Resource  = var.lock_table_arn
        Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["*/targets/*"] } }
      },
      {
        Sid      = "DenyLockTableBroadReads"
        Effect   = "Deny"
        Action   = ["dynamodb:Scan", "dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:PartiQLSelect"]
        Resource = [var.lock_table_arn, "${var.lock_table_arn}/index/*"]
      },
      {
        Sid       = "DenyLockItemsOutsideTargets"
        Effect    = "Deny"
        Action    = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"]
        Resource  = var.lock_table_arn
        Condition = { "ForAllValues:StringNotLike" = { "dynamodb:LeadingKeys" = ["*/targets/*"] } }
      },
      {
        Sid      = "TerraformPlanTimeIamReadsScoped"
        Effect   = "Allow"
        Action   = local.terraform_plan_time_iam_read_scoped_actions
        Resource = local.terraform_plan_time_iam_read_scoped_resources
      },
      {
        Sid      = "TerraformPlanTimeIamReadsWildcard"
        Effect   = "Allow"
        Action   = local.terraform_plan_time_iam_read_wildcard_actions
        Resource = "*"
      },
      {
        Sid      = "DenyListBucketWithoutTargetPrefix"
        Effect   = "Deny"
        Action   = ["s3:ListBucket"]
        Resource = var.state_bucket_arn
        Condition = {
          Null = {
            "s3:prefix" = "true"
          }
        }
      },
      {
        Sid      = "DenyListBucketOutsideTargetsPrefix"
        Effect   = "Deny"
        Action   = ["s3:ListBucket"]
        Resource = var.state_bucket_arn
        Condition = {
          StringNotLike = {
            "s3:prefix" = "targets/*"
          }
        }
      },
      {
        Sid    = "DenyControlPlaneStateAndSourceRecord"
        Effect = "Deny"
        Action = ["s3:*"]
        Resource = [
          "${var.state_bucket_arn}/source/*",
          "${var.state_bucket_arn}/engine/*",
          "${var.state_bucket_arn}/bootstrap/*",
          "${var.state_bucket_arn}/foundation/*",
          "${var.state_bucket_arn}/deploy/*",
          "${var.state_bucket_arn}/target-connect/*",
          "${var.state_bucket_arn}/target-connect-readonly/*",
          "${var.state_bucket_arn}/target-connect-poweruser/*",
          "${var.state_bucket_arn}/engine-00-bootstrap/*",
          "${var.state_bucket_arn}/engine-02-deploy/*",
        ]
      },
      {
        Sid    = "DenyStateBucketNonBackendPrimitives"
        Effect = "Deny"
        NotAction = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
          "s3:GetBucketLocation",
          "s3:GetBucketVersioning",
        ]
        Resource = [
          var.state_bucket_arn,
          "${var.state_bucket_arn}/*",
        ]
      },
      {
        Sid    = "DenyInfrastructureMutationOutsideStateAndLock"
        Effect = "Deny"
        Action = concat(
          [
            "iam:Create*", "iam:Delete*", "iam:Put*", "iam:Update*",
            "iam:Attach*", "iam:Detach*", "iam:Add*", "iam:Remove*",
            "iam:Tag*", "iam:Untag*", "iam:PassRole", "iam:CreateServiceLinkedRole",
            "iam:ChangePassword", "iam:DeactivateMFADevice", "iam:EnableMFADevice",
            "iam:ResyncMFADevice", "iam:Upload*", "iam:Import*", "iam:Set*",
            "cloudformation:*",
          ],
          [
            "ec2:Run*", "ec2:Terminate*", "ec2:Create*", "ec2:Delete*", "ec2:Modify*",
            "s3:CreateBucket", "s3:DeleteBucket", "s3:PutBucket*",
            "dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:UpdateTable",
          ],
        )
        NotResource = [
          var.state_bucket_arn,
          "${var.state_bucket_arn}/*",
          var.lock_table_arn,
        ]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "executor_readonly_read_only" {
  role       = aws_iam_role.executor_readonly.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}
