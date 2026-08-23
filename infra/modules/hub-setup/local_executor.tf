# executor-local role — legacy same-account executor retained for pre-split state upgrades.

resource "aws_iam_role" "executor_local" {
  count = var.provision_legacy_executor_local ? 1 : 0
  name  = "${var.role_prefix}-executor-local"

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
          "aws:PrincipalArn" = [aws_iam_role.hub_lambda_exec.arn, local.prepare_role_arn]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "executor_local" {
  count = var.provision_legacy_executor_local ? 1 : 0
  name  = "${var.role_prefix}-executor-local-policy"
  role  = aws_iam_role.executor_local[0].id

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
        Sid      = "DenyProtectedHubResources"
        Effect   = "Deny"
        Action   = "*"
        Resource = local.protected_hub_resource_arns
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

resource "aws_iam_role_policy_attachment" "executor_local_read_only" {
  count      = var.provision_legacy_executor_local && !var.enable_apply ? 1 : 0
  role       = aws_iam_role.executor_local[0].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy_attachment" "executor_local_power_user" {
  count      = var.provision_legacy_executor_local && var.enable_apply ? 1 : 0
  role       = aws_iam_role.executor_local[0].name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}
