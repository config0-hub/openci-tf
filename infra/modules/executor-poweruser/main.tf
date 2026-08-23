# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# executor-poweruser role — apply/destroy lanes only; never receives ReadOnlyAccess.

locals {
  executor_poweruser_role_name = "${var.role_prefix}-executor-poweruser"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  hub_account_id                    = try(regex("^arn:aws:iam::([0-9]{12}):role/[^:/]+$", var.hub_lambda_exec_role_arn)[0], "")
  expected_hub_lambda_exec_role_arn = "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-hub-lambda-exec"
  target_account_id                 = data.aws_caller_identity.current.account_id
  target_iam_role_arns              = "arn:aws:iam::${local.target_account_id}:role/*"
  target_iam_instance_profile_arns  = "arn:aws:iam::${local.target_account_id}:instance-profile/*"
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
  terraform_workload_iam_role_lifecycle_actions = [
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:UpdateAssumeRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:PassRole",
  ]
  terraform_workload_iam_instance_profile_lifecycle_actions = [
    "iam:CreateInstanceProfile",
    "iam:DeleteInstanceProfile",
    "iam:AddRoleToInstanceProfile",
    "iam:RemoveRoleFromInstanceProfile",
    "iam:TagInstanceProfile",
    "iam:UntagInstanceProfile",
  ]
  external_id = "openci-tf-${substr(sha256("openci-tf:${local.hub_account_id}:${local.target_account_id}"), 0, 16)}"
  hub_role_prefix_arns = [
    "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-hub-lambda-exec",
    "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-run-folder-apply-prepare-and-submit",
    "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-run-folder-destroy-prepare-and-submit",
  ]
  protected_hub_bucket_arns = [
    for name in concat(
      local.target_account_id == local.hub_account_id ? [] : [
        "${var.role_prefix}-state-${local.hub_account_id}",
      ],
      [
        "${var.role_prefix}-tmp-${local.hub_account_id}",
        "${var.role_prefix}-package-${local.hub_account_id}",
        "${var.role_prefix}-done-${local.hub_account_id}",
      ],
    ) : "arn:aws:s3:::${name}"
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

resource "aws_iam_role" "executor_poweruser" {
  name = local.executor_poweruser_role_name

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

resource "aws_iam_role_policy" "executor_poweruser" {
  name = "${local.executor_poweruser_role_name}-policy"
  role = aws_iam_role.executor_poweruser.id

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
        Sid    = "DenyLockTableNonBackendPrimitives"
        Effect = "Deny"
        NotAction = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem",
          "dynamodb:UpdateItem", "dynamodb:DescribeTable",
        ]
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
        Sid      = "TerraformWorkloadIamRoleLifecycle"
        Effect   = "Allow"
        Action   = local.terraform_workload_iam_role_lifecycle_actions
        Resource = local.target_iam_role_arns
      },
      {
        Sid      = "TerraformWorkloadIamInstanceProfileLifecycle"
        Effect   = "Allow"
        Action   = local.terraform_workload_iam_instance_profile_lifecycle_actions
        Resource = local.target_iam_instance_profile_arns
      },
      {
        Sid    = "DenyIamLifecycleOutsideWorkloadResources"
        Effect = "Deny"
        Action = concat(
          local.terraform_workload_iam_role_lifecycle_actions,
          local.terraform_workload_iam_instance_profile_lifecycle_actions,
        )
        NotResource = [
          local.target_iam_role_arns,
          local.target_iam_instance_profile_arns,
        ]
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
        Sid    = "DenyIamAndCloudFormationUnconditionally"
        Effect = "Deny"
        Action = [
          "iam:AddClientIDToOpenIDConnectProvider",
          "iam:AttachGroupPolicy",
          "iam:AttachUserPolicy",
          "iam:ChangePassword",
          "iam:CreateAccessKey",
          "iam:CreateAccountAlias",
          "iam:CreateGroup",
          "iam:CreateLoginProfile",
          "iam:CreateOpenIDConnectProvider",
          "iam:CreatePolicy",
          "iam:CreatePolicyVersion",
          "iam:CreateSAMLProvider",
          "iam:CreateServiceLinkedRole",
          "iam:CreateUser",
          "iam:CreateVirtualMFADevice",
          "iam:DeactivateMFADevice",
          "iam:DeleteAccessKey",
          "iam:DeleteAccountAlias",
          "iam:DeleteGroup",
          "iam:DeleteLoginProfile",
          "iam:DeleteOpenIDConnectProvider",
          "iam:DeletePolicy",
          "iam:DeletePolicyVersion",
          "iam:DeleteSAMLProvider",
          "iam:DeleteSSHPublicKey",
          "iam:DeleteServerCertificate",
          "iam:DeleteServiceLinkedRole",
          "iam:DeleteSigningCertificate",
          "iam:DeleteUser",
          "iam:DeleteVirtualMFADevice",
          "iam:DetachGroupPolicy",
          "iam:DetachUserPolicy",
          "iam:EnableMFADevice",
          "iam:Import*",
          "iam:Put*",
          "iam:RemoveClientIDFromOpenIDConnectProvider",
          "iam:Reset*",
          "iam:ResyncMFADevice",
          "iam:Set*",
          "iam:UpdateAccessKey",
          "iam:UpdateAccountPasswordPolicy",
          "iam:UpdateGroup",
          "iam:UpdateLoginProfile",
          "iam:UpdateOpenIDConnectProviderThumbprint",
          "iam:UpdateSAMLProvider",
          "iam:UpdateSSHPublicKey",
          "iam:UpdateServerCertificate",
          "iam:UpdateSigningCertificate",
          "iam:UpdateUser",
          "iam:Upload*",
          "cloudformation:*",
        ]
        Resource = "*"
      },
      {
        Sid      = "DenyProtectedHubResources"
        Effect   = "Deny"
        Action   = "*"
        Resource = local.protected_hub_resource_arns
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
    ]
  })
}

resource "aws_iam_role_policy_attachment" "executor_poweruser_power_user" {
  role       = aws_iam_role.executor_poweruser.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}
