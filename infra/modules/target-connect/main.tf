# executor-remote role — deployed in target accounts, assumed by hub-lambda-exec

# ref 4353245 - openci-tf remote executor consistency naming
locals {
  executor_remote_role_name = "${var.role_prefix}-executor-remote"
}

data "aws_caller_identity" "current" {}

locals {
  # Trust the hub CALLER roles (the run-folder prepare-and-submit Lambda role
  # and the hub-lambda-exec role) via hub-account-root principal +
  # aws:PrincipalArn condition: works cross-account, and role re-creation in
  # the hub does not invalidate the trust.
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
  external_id = "openci-tf-${substr(sha256("openci-tf:${local.hub_account_id}:${local.target_account_id}"), 0, 16)}"
  hub_role_prefix_arns = [
    "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-hub-lambda-exec",
    "arn:aws:iam::${local.hub_account_id}:role/${var.role_prefix}-run-folder-prepare-and-submit",
  ]
}

resource "aws_iam_role" "executor_remote" {
  count = var.provision_legacy_executor_remote ? 1 : 0
  name  = local.executor_remote_role_name

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

resource "aws_iam_role_policy" "executor_remote" {
  count = var.provision_legacy_executor_remote ? 1 : 0
  name  = "${local.executor_remote_role_name}-policy"
  role  = aws_iam_role.executor_remote[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Executor state lives ONLY under the targets/ prefix; when the state
        # bucket is shared with the install control-plane (single-account),
        # install state, source record, and engine artifacts stay unreachable.
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
        # With enable_apply the executor must mutate infrastructure (Terraform
        # apply/destroy), so only the privilege-escalation guards (IAM,
        # CloudFormation) stay denied. Without it, the full read-only-era deny
        # list is preserved bit-for-bit.
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
          var.enable_apply ? [] : [
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

resource "aws_iam_role_policy_attachment" "executor_remote_read_only" {
  count      = var.provision_legacy_executor_remote && !var.enable_apply ? 1 : 0
  role       = aws_iam_role.executor_remote[0].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy_attachment" "executor_remote_power_user" {
  count      = var.provision_legacy_executor_remote && var.enable_apply ? 1 : 0
  role       = aws_iam_role.executor_remote[0].name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}
