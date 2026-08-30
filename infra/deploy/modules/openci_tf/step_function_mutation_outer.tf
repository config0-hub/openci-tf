# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
locals {
  mutation_outer_shared_terminal_states = {
    RenderPR = {
      Type     = "Task"
      Resource = local.lambda_arns["render-pr"]
      Parameters = {
        "webhook_info.$"           = "$.webhook_info"
        "settings.$"               = "$.settings"
        "action.$"                 = "$.action"
        "run_id.$"                 = "$.run_id"
        "deadline_at.$"            = "$.deadline_at"
        "notification_target.$"    = "$.notification_target"
        "outcomes.$"               = "$.outcomes"
        "skipped.$"                = "$.skipped"
        "no_op_reason.$"           = "$.no_op_reason"
        "folders.$"                = "$.folders"
        "all_flag.$"               = "$.all_flag"
        "affected_flag.$"          = "$.affected_flag"
        "requested_comment_id.$"   = "$.requested_comment_id"
        "requested_comment_body.$" = "$.requested_comment_body"
        "intent_comment_id.$"      = "$.intent_comment_id"
        "consumed_confirm_token.$" = "$.consumed_confirm_token"
        "execution_arn.$"          = "$$.Execution.Id"
      }
      ResultPath = "$.render_flags"
      Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.render_error", Next = "FailRenderPR" }]
      Next       = "RouteAfterRender"
    }
    FailRenderPR = {
      Type       = "Pass"
      Result     = { failed_step = "RenderPR" }
      ResultPath = "$.pipeline_failure"
      Next       = "RenderPRFailureComment"
    }
    RenderPRFailureComment = {
      Type     = "Task"
      Resource = local.lambda_arns["render-pr"]
      Parameters = {
        "pipeline_failure.$"       = "$.pipeline_failure"
        "webhook_info.$"           = "$.webhook_info"
        "settings.$"               = "$.settings"
        "action.$"                 = "$.action"
        "run_id.$"                 = "$.run_id"
        "notification_target.$"    = "$.notification_target"
        "folders.$"                = "$.folders"
        "all_flag.$"               = "$.all_flag"
        "affected_flag.$"          = "$.affected_flag"
        "requested_comment_id.$"   = "$.requested_comment_id"
        "requested_comment_body.$" = "$.requested_comment_body"
        "intent_comment_id.$"      = "$.intent_comment_id"
        "consumed_confirm_token.$" = "$.consumed_confirm_token"
        "confirm_token.$"          = "$.confirm_token"
        "execution_arn.$"          = "$$.Execution.Id"
      }
      ResultPath = null
      Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizeAfterRenderFailure" }]
      Next       = "FinalizeAfterRenderFailure"
    }
    RouteAfterRender = {
      Type = "Choice"
      Choices = [
        {
          And = [
            { Variable = "$.config_resolution_failed", IsPresent = true },
            { Variable = "$.config_resolution_failed", BooleanEquals = true },
          ]
          Next = "FinalizeRun"
        },
        {
          And = [
            { Variable = "$.render_flags.execution_failed", IsPresent = true },
            { Variable = "$.render_flags.execution_failed", BooleanEquals = true },
          ]
          Next = "FinalizeRun"
        },
      ]
      Default = "Done"
    }
    FinalizeAfterRenderFailure = {
      Type       = "Task"
      Resource   = local.lambda_arns["finalize-run"]
      ResultPath = null
      Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizeRunFailed" }]
      Next       = "RenderPRFailed"
    }
    RenderPRFailed = {
      Type  = "Fail"
      Error = "RenderPRFailed"
      Cause = "final PR render failed"
    }
    RenderPipelineFailure = {
      Type     = "Task"
      Resource = local.lambda_arns["render-pr"]
      Parameters = {
        "pipeline_failure.$"       = "$.pipeline_failure"
        "webhook_info.$"           = "$.webhook_info"
        "settings.$"               = "$.settings"
        "action.$"                 = "$.action"
        "run_id.$"                 = "$.run_id"
        "notification_target.$"    = "$.notification_target"
        "folders.$"                = "$.folders"
        "all_flag.$"               = "$.all_flag"
        "affected_flag.$"          = "$.affected_flag"
        "requested_comment_id.$"   = "$.requested_comment_id"
        "requested_comment_body.$" = "$.requested_comment_body"
        "intent_comment_id.$"      = "$.intent_comment_id"
        "consumed_confirm_token.$" = "$.consumed_confirm_token"
        "confirm_token.$"          = "$.confirm_token"
        "execution_arn.$"          = "$$.Execution.Id"
      }
      ResultPath = null
      Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizeRun" }]
      Next       = "FinalizeRun"
    }
    FinalizeRun = {
      Type       = "Task"
      Resource   = local.lambda_arns["finalize-run"]
      ResultPath = null
      Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizeRunFailed" }]
      Next       = "PipelineFailed"
    }
    PipelineFailed = {
      Type  = "Fail"
      Error = "PipelineFailed"
      Cause = "openci-tf mutation pipeline failed"
    }
    FinalizeRunFailed = {
      Type  = "Fail"
      Error = "FinalizeRunFailed"
      Cause = "registry finalization failed"
    }
    Done = { Type = "Pass", End = true }
  }
}

resource "aws_iam_role" "stepfunction_apply" {
  name = "${var.project_name}-stepfunction-apply-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role" "stepfunction_destroy" {
  name = "${var.project_name}-stepfunction-destroy-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "stepfunction_apply" {
  name = "${var.project_name}-stepfunction-apply-policy"
  role = aws_iam_role.stepfunction_apply.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = values(local.lambda_arns) },
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = var.run_folder_apply_state_machine_arn },
      { Effect = "Allow", Action = ["states:DescribeExecution", "states:StopExecution"], Resource = "arn:aws:states:*:*:execution:${element(split(":", var.run_folder_apply_state_machine_arn), 6)}:*" },
      { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = "arn:aws:events:*:*:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule" },
      { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*" },
    ]
  })
}

resource "aws_iam_role_policy" "stepfunction_destroy" {
  name = "${var.project_name}-stepfunction-destroy-policy"
  role = aws_iam_role.stepfunction_destroy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = values(local.lambda_arns) },
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = var.run_folder_destroy_state_machine_arn },
      { Effect = "Allow", Action = ["states:DescribeExecution", "states:StopExecution"], Resource = "arn:aws:states:*:*:execution:${element(split(":", var.run_folder_destroy_state_machine_arn), 6)}:*" },
      { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = "arn:aws:events:*:*:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule" },
      { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*" },
    ]
  })
}

resource "aws_cloudwatch_log_group" "step_function_apply" {
  name              = "/aws/vendedlogs/states/${var.project_name}-apply"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "step_function_destroy" {
  name              = "/aws/vendedlogs/states/${var.project_name}-destroy"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_sfn_state_machine" "openci_tf_apply" {
  name     = "${var.project_name}-apply"
  role_arn = aws_iam_role.stepfunction_apply.arn
  definition = jsonencode({
    StartAt = "ParseCommand"
    States = merge(local.mutation_outer_shared_terminal_states, {
      ParseCommand = { Type = "Task", Resource = local.lambda_arns["parse-command"], Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailParseCommand" }], Next = "RouteAction" }
      RouteAction = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.intent_confirm", IsPresent = true },
            { Variable = "$.intent_confirm", BooleanEquals = true },
            { Variable = "$.action", StringEquals = "apply" },
          ]
          Next = "ConfirmApplyIntent"
        }]
        Default = "FailRouteAction"
      }
      ConfirmApplyIntent = {
        Type     = "Task"
        Resource = local.lambda_arns["intent-confirm"]
        Catch    = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailConfirmApplyIntent" }]
        Next     = "RouteAfterConfirm"
      }
      RouteAfterConfirm = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.intent_failed", IsPresent = true },
            { Variable = "$.intent_failed", BooleanEquals = true },
          ]
          Next = "IntentFailed"
        }]
        Default = "ValidateAndResolve"
      }
      IntentFailed = { Type = "Fail", Error = "IntentFailed", Cause = "apply intent gate failed" }
      ValidateAndResolve = {
        Type     = "Task"
        Resource = local.lambda_arns["validate-and-resolve"]
        Catch = [
          { ErrorEquals = ["ConfigResolutionError"], ResultPath = null, Next = "NormalizeConfigError" },
          { ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailValidateAndResolve" },
        ]
        Next = "RenderPlaceholder"
      }
      # A Catch transition must target a state. This Task remains as that
      # envelope while the render Lambda owns the bounded result shaping.
      NormalizeConfigError = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          normalize_config_error = true
          "state.$"              = "$"
        }
        Retry = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"], IntervalSeconds = 1, MaxAttempts = 3, BackoffRate = 2.0 }]
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailValidateAndResolve" }]
        Next  = "RenderPR"
      }
      RenderPlaceholder = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          placeholder                = true
          "webhook_info.$"           = "$.webhook_info"
          "settings.$"               = "$.settings"
          "action.$"                 = "$.action"
          "run_id.$"                 = "$.run_id"
          "notification_target.$"    = "$.notification_target"
          "map_items.$"              = "$.map_items"
          "skipped.$"                = "$.skipped"
          "folders.$"                = "$.folders"
          "all_flag.$"               = "$.all_flag"
          "affected_flag.$"          = "$.affected_flag"
          "requested_comment_id.$"   = "$.requested_comment_id"
          "requested_comment_body.$" = "$.requested_comment_body"
          "intent_comment_id.$"      = "$.intent_comment_id"
          "consumed_confirm_token.$" = "$.consumed_confirm_token"
          "execution_arn.$"          = "$$.Execution.Id"
          outcomes                   = []
        }
        ResultPath = null
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "RunFoldersSequential" }]
        Next       = "RunFoldersSequential"
      }
      RunFoldersSequential = {
        Type           = "Map"
        ItemsPath      = "$.map_items"
        MaxConcurrency = 1
        ResultPath     = "$.outcomes"
        ItemSelector = {
          "run_id.$"                     = "$$.Map.Item.Value.run_id"
          "folder.$"                     = "$$.Map.Item.Value.folder"
          "account_id.$"                 = "$$.Map.Item.Value.account_id"
          "account_binding.$"            = "$$.Map.Item.Value.b"
          "action.$"                     = "$$.Map.Item.Value.action"
          "attempt.$"                    = "$$.Map.Item.Value.attempt"
          "budget.$"                     = "$$.Map.Item.Value.budget"
          "deadline_at.$"                = "$$.Map.Item.Value.deadline_at"
          "step_index.$"                 = "$.step_index"
          "pipeline_plan_focus.$"        = "$$.Map.Item.Value.pipeline_plan_focus"
          "folder_config.$"              = "$$.Map.Item.Value.c"
          "execution_id.$"               = "$$.Map.Item.Value.e"
          "folder_pin.$"                 = "$$.Map.Item.Value.folder_pin"
          "source_plan_run_id.$"         = "$$.Map.Item.Value.source_plan_run_id"
          "grace_seconds.$"              = "$$.Map.Item.Value.grace_seconds"
          "command_context.$"            = "$$.Map.Item.Value.command_context"
          "upstream_urls.$"              = "$.map_shared.upstream_urls"
          "repo_name.$"                  = "$.map_shared.repo_name"
          "git_url.$"                    = "$.map_shared.git_url"
          "commit_hash.$"                = "$.map_shared.commit_hash"
          "ssm_openci_tf_github_token.$" = "$.map_shared.ssm_openci_tf_github_token"
          "ssm_infracost_api_key.$"      = "$.map_shared.ssm_infracost_api_key"
        }
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailRunFolders" }]
        Iterator = {
          StartAt = "GraceWait"
          States = {
            GraceWait = {
              Type        = "Wait"
              SecondsPath = "$.grace_seconds"
              Next        = "SequentialRunFolder"
            }
            SequentialRunFolder = {
              Type       = "Task"
              Resource   = "arn:aws:states:::states:startExecution.sync:2"
              Parameters = { "StateMachineArn" = var.run_folder_apply_state_machine_arn, "Input.$" = "$" }
              ResultPath = "$.child_execution"
              Next       = "SequentialRouteChildOutcome"
            }
            SequentialRouteChildOutcome = {
              Type = "Choice"
              Choices = [{
                And = [
                  { Variable = "$.child_execution.Output", IsPresent = true },
                  { Variable = "$.child_execution.Output.exec_id", IsPresent = true },
                  { Variable = "$.child_execution.Output.succeeded", IsPresent = true },
                  { Variable = "$.child_execution.Output.succeeded", BooleanEquals = true },
                ]
                Next = "SequentialNormalizeFolderOutcome"
              }]
              Default = "SequentialFailFolderIteration"
            }
            SequentialNormalizeFolderOutcome = {
              Type     = "Task"
              Resource = local.lambda_arns["render-pr"]
              Parameters = {
                normalize_folder_outcome = true
                "state.$"                = "$"
              }
              End = true
            }
            SequentialFailFolderIteration = { Type = "Fail", Error = "FolderExecutionFailed", Cause = "sequential folder execution failed" }
          }
        }
        Next = "RenderPR"
      }
      FailParseCommand       = { Type = "Pass", Result = { failed_step = "ParseCommand" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailRouteAction        = { Type = "Pass", Result = { failed_step = "RouteAction" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailConfirmApplyIntent = { Type = "Pass", Result = { failed_step = "ConfirmApplyIntent" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailValidateAndResolve = { Type = "Pass", Result = { failed_step = "ValidateAndResolve" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailRunFolders         = { Type = "Pass", Result = { failed_step = "RunFoldersSequential" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
    })
  })
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_function_apply.arn}:*"
    level                  = "ERROR"
    include_execution_data = false
  }
  tags = merge(var.tags, { Name = "${var.project_name}-apply" })
}

resource "aws_sfn_state_machine" "openci_tf_destroy" {
  name     = "${var.project_name}-destroy"
  role_arn = aws_iam_role.stepfunction_destroy.arn
  definition = jsonencode({
    StartAt = "ParseCommand"
    States = merge(local.mutation_outer_shared_terminal_states, {
      ParseCommand = { Type = "Task", Resource = local.lambda_arns["parse-command"], Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailParseCommand" }], Next = "RouteAction" }
      RouteAction = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.intent_confirm", IsPresent = true },
            { Variable = "$.intent_confirm", BooleanEquals = true },
            { Variable = "$.action", StringEquals = "destroy" },
          ]
          Next = "ConfirmDestroyIntent"
        }]
        Default = "FailRouteAction"
      }
      ConfirmDestroyIntent = {
        Type     = "Task"
        Resource = local.lambda_arns["intent-confirm"]
        Catch    = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailConfirmDestroyIntent" }]
        Next     = "RouteAfterConfirm"
      }
      RouteAfterConfirm = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.intent_failed", IsPresent = true },
            { Variable = "$.intent_failed", BooleanEquals = true },
          ]
          Next = "IntentFailed"
        }]
        Default = "ValidateAndResolve"
      }
      IntentFailed = { Type = "Fail", Error = "IntentFailed", Cause = "destroy intent gate failed" }
      ValidateAndResolve = {
        Type     = "Task"
        Resource = local.lambda_arns["validate-and-resolve"]
        Catch = [
          { ErrorEquals = ["ConfigResolutionError"], ResultPath = null, Next = "NormalizeConfigError" },
          { ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailValidateAndResolve" },
        ]
        Next = "RenderPlaceholder"
      }
      # A Catch transition must target a state. This Task remains as that
      # envelope while the render Lambda owns the bounded result shaping.
      NormalizeConfigError = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          normalize_config_error = true
          "state.$"              = "$"
        }
        Retry = [{ ErrorEquals = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"], IntervalSeconds = 1, MaxAttempts = 3, BackoffRate = 2.0 }]
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailValidateAndResolve" }]
        Next  = "RenderPR"
      }
      RenderPlaceholder = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          placeholder                = true
          "webhook_info.$"           = "$.webhook_info"
          "settings.$"               = "$.settings"
          "action.$"                 = "$.action"
          "run_id.$"                 = "$.run_id"
          "notification_target.$"    = "$.notification_target"
          "map_items.$"              = "$.map_items"
          "skipped.$"                = "$.skipped"
          "folders.$"                = "$.folders"
          "all_flag.$"               = "$.all_flag"
          "affected_flag.$"          = "$.affected_flag"
          "requested_comment_id.$"   = "$.requested_comment_id"
          "requested_comment_body.$" = "$.requested_comment_body"
          "intent_comment_id.$"      = "$.intent_comment_id"
          "consumed_confirm_token.$" = "$.consumed_confirm_token"
          "execution_arn.$"          = "$$.Execution.Id"
          outcomes                   = []
        }
        ResultPath = null
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "RunFoldersSequential" }]
        Next       = "RunFoldersSequential"
      }
      RunFoldersSequential = {
        Type           = "Map"
        ItemsPath      = "$.map_items"
        MaxConcurrency = 1
        ResultPath     = "$.outcomes"
        ItemSelector = {
          "run_id.$"                     = "$$.Map.Item.Value.run_id"
          "folder.$"                     = "$$.Map.Item.Value.folder"
          "account_id.$"                 = "$$.Map.Item.Value.account_id"
          "account_binding.$"            = "$$.Map.Item.Value.b"
          "action.$"                     = "$$.Map.Item.Value.action"
          "attempt.$"                    = "$$.Map.Item.Value.attempt"
          "budget.$"                     = "$$.Map.Item.Value.budget"
          "deadline_at.$"                = "$$.Map.Item.Value.deadline_at"
          "step_index.$"                 = "$.step_index"
          "pipeline_plan_focus.$"        = "$$.Map.Item.Value.pipeline_plan_focus"
          "folder_config.$"              = "$$.Map.Item.Value.c"
          "execution_id.$"               = "$$.Map.Item.Value.e"
          "folder_pin.$"                 = "$$.Map.Item.Value.folder_pin"
          "source_plan_run_id.$"         = "$$.Map.Item.Value.source_plan_run_id"
          "grace_seconds.$"              = "$$.Map.Item.Value.grace_seconds"
          "command_context.$"            = "$$.Map.Item.Value.command_context"
          "upstream_urls.$"              = "$.map_shared.upstream_urls"
          "repo_name.$"                  = "$.map_shared.repo_name"
          "git_url.$"                    = "$.map_shared.git_url"
          "commit_hash.$"                = "$.map_shared.commit_hash"
          "ssm_openci_tf_github_token.$" = "$.map_shared.ssm_openci_tf_github_token"
          "ssm_infracost_api_key.$"      = "$.map_shared.ssm_infracost_api_key"
        }
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailRunFolders" }]
        Iterator = {
          StartAt = "GraceWait"
          States = {
            GraceWait = {
              Type        = "Wait"
              SecondsPath = "$.grace_seconds"
              Next        = "SequentialRunFolder"
            }
            SequentialRunFolder = {
              Type       = "Task"
              Resource   = "arn:aws:states:::states:startExecution.sync:2"
              Parameters = { "StateMachineArn" = var.run_folder_destroy_state_machine_arn, "Input.$" = "$" }
              ResultPath = "$.child_execution"
              Next       = "SequentialRouteChildOutcome"
            }
            SequentialRouteChildOutcome = {
              Type = "Choice"
              Choices = [{
                And = [
                  { Variable = "$.child_execution.Output", IsPresent = true },
                  { Variable = "$.child_execution.Output.exec_id", IsPresent = true },
                  { Variable = "$.child_execution.Output.succeeded", IsPresent = true },
                  { Variable = "$.child_execution.Output.succeeded", BooleanEquals = true },
                ]
                Next = "SequentialNormalizeFolderOutcome"
              }]
              Default = "SequentialFailFolderIteration"
            }
            SequentialNormalizeFolderOutcome = {
              Type     = "Task"
              Resource = local.lambda_arns["render-pr"]
              Parameters = {
                normalize_folder_outcome = true
                "state.$"                = "$"
              }
              End = true
            }
            SequentialFailFolderIteration = { Type = "Fail", Error = "FolderExecutionFailed", Cause = "sequential folder execution failed" }
          }
        }
        Next = "RenderPR"
      }
      FailParseCommand         = { Type = "Pass", Result = { failed_step = "ParseCommand" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailRouteAction          = { Type = "Pass", Result = { failed_step = "RouteAction" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailConfirmDestroyIntent = { Type = "Pass", Result = { failed_step = "ConfirmDestroyIntent" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailValidateAndResolve   = { Type = "Pass", Result = { failed_step = "ValidateAndResolve" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
      FailRunFolders           = { Type = "Pass", Result = { failed_step = "RunFoldersSequential" }, ResultPath = "$.pipeline_failure", Next = "RenderPipelineFailure" }
    })
  })
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_function_destroy.arn}:*"
    level                  = "ERROR"
    include_execution_data = false
  }
  tags = merge(var.tags, { Name = "${var.project_name}-destroy" })
}
