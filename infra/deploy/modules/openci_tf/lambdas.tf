# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
locals {
  lambdas = {
    trigger-stepf = {
      handler     = "src.services.webhook.handler.handler"
      timeout     = 30
      description = "Webhook receiver: HMAC validation, event filtering, starts Step Function"
    }
    parse-command = {
      handler     = "src.services.resolve.handler.handler"
      timeout     = 30
      description = "Parse PR comment into action + folders"
    }
    validate-and-resolve = {
      handler     = "src.services.resolve.validate_and_resolve.handler"
      timeout     = 300
      description = "Shallow clone, read configs, validate, resolve cmds, build payload"
    }
    render-pr = {
      handler     = "src.services.render.handler.handler"
      timeout     = 300
      description = "Render bounded artifact summaries into PR comments"
    }
    api = {
      handler     = "src.services.api.handler.handler"
      timeout     = 30
      description = "AWS IAM core API for run registry and artifact reads"
    }
    intent-create = {
      handler     = "src.services.intent.handler.create_handler"
      timeout     = 60
      description = "Create apply/destroy intent tokens after gate checks"
    }
    intent-confirm = {
      handler     = "src.services.intent.handler.confirm_handler"
      timeout     = 60
      description = "Confirm apply/destroy intent tokens and enrich execution state"
    }
    finalize-run = {
      handler     = "src.services.orchestration.finalize_run.handler"
      timeout     = 30
      description = "Registry-only finalizer for outer Step Functions failures"
    }
  }
}

# Lambda functions
resource "aws_lambda_function" "functions" {
  for_each = local.lambdas

  function_name = "${var.project_name}-${each.key}"
  role          = each.key == "api" ? aws_iam_role.api_lambda.arn : aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = var.ecr_image_uri
  timeout       = each.value.timeout
  memory_size   = each.key == "validate-and-resolve" ? 512 : 256
  description   = each.value.description

  image_config {
    command = [each.value.handler]
  }

  dynamic "dead_letter_config" {
    for_each = each.key == "trigger-stepf" ? [true] : []
    content { target_arn = aws_sqs_queue.webhook_dlq.arn }
  }

  environment {
    variables = merge(
      {
        PROJECT_NAME               = var.project_name
        STEP_FUNCTION_ARN          = local.step_function_arn
        APPLY_STEP_FUNCTION_ARN    = local.apply_step_function_arn
        DESTROY_STEP_FUNCTION_ARN  = local.destroy_step_function_arn
        LOG_LEVEL                  = "INFO"
        SETTINGS_TABLE_NAME        = aws_dynamodb_table.settings.name
        LOCKS_TABLE_NAME           = aws_dynamodb_table.locks.name
        RUN_REGISTRY_TABLE_NAME    = aws_dynamodb_table.run_registry.name
        RUN_HISTORY_RETENTION_DAYS = tostring(var.run_history_retention_days)
        TMP_BUCKET_NAME            = var.tmp_bucket_name
        DONE_BUCKET_NAME           = var.done_bucket_name
      },
      each.key == "api" ? {
        API_CALLER_POLICY_JSON = jsonencode(var.api_caller_policy_json)
        PACKAGE_BUCKET_NAME    = var.package_bucket_name
      } : {},
      contains(["api", "render-pr", "intent-create"], each.key) ? {
        TMP_LIFECYCLE_DAYS     = tostring(var.tmp_lifecycle_days)
        PACKAGE_LIFECYCLE_DAYS = tostring(var.package_lifecycle_days)
        DONE_LIFECYCLE_DAYS    = tostring(var.done_lifecycle_days)
        PLAN_RETENTION_DAYS    = tostring(var.plan_retention_days)
      } : {},
      contains(["render-pr", "intent-create", "intent-confirm", "finalize-run"], each.key) ? {
        LOCKS_TABLE_NAME = aws_dynamodb_table.locks.name
      } : {},
      each.key == "render-pr" ? {
        ENGINE_CODEBUILD_PROJECT_NAME = local.engine_codebuild_project_name
        ENGINE_CODEBUILD_ACCOUNT_ID   = local.account_id
        AWS_CONSOLE_START_URL         = var.aws_console_start_url
        AWS_CONSOLE_ROLE_NAME         = var.aws_console_role_name
      } : {},
    )
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${each.key}"
  })
}

resource "aws_cloudwatch_log_group" "functions" {
  for_each          = local.lambdas
  name              = "/aws/lambda/${var.project_name}-${each.key}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}
