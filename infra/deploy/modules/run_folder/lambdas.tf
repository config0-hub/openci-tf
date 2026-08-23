# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
locals {
  handlers = {
    "prepare-and-submit"     = "src.services.run_folder.prepare_and_submit.handler"
    "poll-done"              = "src.services.run_folder.poll_done.handler"
    "collect"                = "src.services.run_folder.collect.handler"
    "persist-retry-attempt"  = "src.services.run_folder.persist_retry_attempt.handler"
    "write-failure-manifest" = "src.services.run_folder.write_failure_manifest.handler"
  }
}
resource "aws_lambda_function" "this" {
  for_each      = local.handlers
  function_name = "${local.resource_name_label}-${each.key}"
  role          = aws_iam_role.lambda[each.key].arn
  package_type  = "Image"
  image_uri     = var.ecr_image_uri
  timeout       = each.key == "poll-done" ? 30 : 900
  image_config { command = [each.value] }
  environment {
    variables = merge(
      {
        PROJECT_NAME            = var.project_name
        PACKAGE_BUCKET_NAME     = var.package_bucket_name
        TMP_BUCKET_NAME         = var.tmp_bucket_name
        DONE_BUCKET_NAME        = var.done_bucket_name
        KMS_KEY_ARN             = var.kms_key_arn
        ENGINE_INIT_LAMBDA_NAME = var.engine_init_lambda_name
        LANE_MODE               = var.lane
      },
      local.mutation_lane ? {
        ENGINE_CODEBUILD_STATE_MACHINE_ARN = var.engine_codebuild_state_machine_arn
        ENGINE_CODEBUILD_PROJECT_NAME      = local.engine_codebuild_project_name
        ENGINE_CODEBUILD_ACCOUNT_ID        = data.aws_caller_identity.current.account_id
        AWS_CONSOLE_START_URL              = var.aws_console_start_url
        AWS_CONSOLE_ROLE_NAME              = var.aws_console_role_name
        RUN_REGISTRY_TABLE_NAME            = "${var.project_name}-run-registry"
      } : {},
      contains(["poll-done"], each.key) && local.mutation_lane ? {
        ENGINE_CODEBUILD_PROJECT_NAME = local.engine_codebuild_project_name
      } : {},
      each.key == "persist-retry-attempt" || each.key == "write-failure-manifest" || each.key == "collect" || each.key == "prepare-and-submit" ? {
        RUN_REGISTRY_TABLE_NAME    = "${var.project_name}-run-registry"
        RUN_HISTORY_RETENTION_DAYS = tostring(var.run_history_retention_days)
      } : {},
      contains(["collect", "write-failure-manifest", "prepare-and-submit"], each.key) ? {
        TMP_LIFECYCLE_DAYS     = tostring(var.tmp_lifecycle_days)
        PACKAGE_LIFECYCLE_DAYS = tostring(var.package_lifecycle_days)
        DONE_LIFECYCLE_DAYS    = tostring(var.done_lifecycle_days)
        PLAN_RETENTION_DAYS    = tostring(var.plan_retention_days)
      } : {},
    )
  }
}

resource "aws_cloudwatch_log_group" "functions" {
  for_each          = local.handlers
  name              = "/aws/lambda/${local.resource_name_label}-${each.key}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}
