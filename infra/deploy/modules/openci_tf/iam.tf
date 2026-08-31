# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# --- Lambda IAM ---

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role" "api_lambda" {
  name = "${var.project_name}-api-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect = "Allow"
          Action = [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
          ]
          Resource = "arn:aws:logs:*:*:*"
        },
        {
          Effect = "Allow"
          Action = [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:Query",
            "dynamodb:DeleteItem",
            "dynamodb:TransactWriteItems",
          ]
          Resource = [
            aws_dynamodb_table.settings.arn,
            aws_dynamodb_table.locks.arn,
            aws_dynamodb_table.run_registry.arn,
            "${aws_dynamodb_table.run_registry.arn}/index/repo_created",
            "${aws_dynamodb_table.run_registry.arn}/index/pipeline_apply_step",
          ]
        },
        {
          Effect = "Allow"
          Action = [
            "ssm:GetParameter",
          ]
          Resource = "arn:aws:ssm:*:*:parameter/${var.project_name}/*"
        },
        {
          Effect = "Allow"
          Action = [
            "states:StartExecution",
            "states:DescribeExecution",
          ]
          Resource = [
            local.step_function_arn,
            "${replace(local.step_function_arn, ":stateMachine:", ":execution:")}:*",
            local.apply_step_function_arn,
            "${replace(local.apply_step_function_arn, ":stateMachine:", ":execution:")}:*",
            local.destroy_step_function_arn,
            "${replace(local.destroy_step_function_arn, ":stateMachine:", ":execution:")}:*",
          ]
        },
        {
          Effect   = "Allow"
          Action   = ["codebuild:BatchGetBuilds", "codebuild:ListBuildsForProject"]
          Resource = "arn:aws:codebuild:${local.region}:${local.account_id}:project/${local.engine_codebuild_project_name}"
        },
        {
          Effect   = "Allow"
          Action   = ["s3:GetObject"]
          Resource = "${var.tmp_bucket_arn}/openci-tf/*"
        },
        {
          Effect   = "Allow"
          Action   = ["s3:PutObject"]
          Resource = "${var.tmp_bucket_arn}/openci-tf/*/pr-*/report-all.env"
        },
        # ListBucket on the tmp bucket is required for render list_text_prefix; execution
        # IDs are runtime-generated so only a bounded prefix wildcard is feasible.
        {
          Effect   = "Allow"
          Action   = "s3:ListBucket"
          Resource = var.tmp_bucket_arn
          Condition = {
            StringLike = {
              "s3:prefix" = ["openci-tf/*"]
            }
          }
        },
        {
          Effect    = "Allow"
          Action    = ["kms:Decrypt"]
          Resource  = var.kms_key_arn
          Condition = merge(local.foundation_kms_context, local.foundation_kms_via_s3)
        },
        {
          Effect    = "Allow"
          Action    = ["kms:GenerateDataKey"]
          Resource  = var.kms_key_arn
          Condition = merge(local.report_all_pointer_kms_context, local.foundation_kms_via_s3)
        },
      ],
      var.engine_init_lambda_arn != "" ? [{
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = var.engine_init_lambda_arn
      }] : [],
      length(var.assume_role_arns) > 0 ? [{
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = var.assume_role_arns
      }] : [],
      [{
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.webhook_dlq.arn
      }],
    )
  })
}

resource "aws_iam_role_policy" "api_lambda" {
  name = "${var.project_name}-api-lambda-policy"
  role = aws_iam_role.api_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:TransactWriteItems",
        ]
        Resource = [
          aws_dynamodb_table.settings.arn,
          aws_dynamodb_table.locks.arn,
          aws_dynamodb_table.run_registry.arn,
          "${aws_dynamodb_table.run_registry.arn}/index/repo_created",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution",
          "states:DescribeExecution",
        ]
        Resource = [
          local.step_function_arn,
          "${replace(local.step_function_arn, ":stateMachine:", ":execution:")}:*",
          local.apply_step_function_arn,
          "${replace(local.apply_step_function_arn, ":stateMachine:", ":execution:")}:*",
          local.destroy_step_function_arn,
          "${replace(local.destroy_step_function_arn, ":stateMachine:", ":execution:")}:*",
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${var.tmp_bucket_arn}/*",
          "${var.done_bucket_arn}/*",
        ]
      },
      {
        Effect    = "Allow"
        Action    = ["kms:Decrypt"]
        Resource  = var.kms_key_arn
        Condition = merge(local.foundation_kms_context, local.foundation_kms_via_s3)
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "api_lambda_basic" {
  role       = aws_iam_role.api_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --- Step Function IAM ---

resource "aws_iam_role" "stepfunction" {
  name = "${var.project_name}-stepfunction-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "states.amazonaws.com"
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "stepfunction" {
  name = "${var.project_name}-stepfunction-policy"
  role = aws_iam_role.stepfunction.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = values(local.lambda_arns) },
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = var.run_folder_state_machine_arn },
      { Effect = "Allow", Action = ["states:DescribeExecution", "states:StopExecution"], Resource = "arn:aws:states:*:*:execution:${element(split(":", var.run_folder_state_machine_arn), 6)}:*" },
      { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = "arn:aws:events:*:*:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule" },
      # Step Functions logging to CloudWatch requires log-delivery permissions (resource-level scoping unsupported).
      { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*" },
    ]
  })
}
