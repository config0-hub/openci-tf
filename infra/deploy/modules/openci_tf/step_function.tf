# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
resource "aws_sfn_state_machine" "openci_tf" {
  name     = var.project_name
  role_arn = aws_iam_role.stepfunction.arn
  definition = jsonencode({
    StartAt = "ParseCommand"
    States = {
      ParseCommand = { Type = "Task", Resource = local.lambda_arns["parse-command"], Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailParseCommand" }], Next = "RouteAction" }
      RouteAction = {
        Type = "Choice"
        Choices = [
          { And = [{ Variable = "$.intent_create", IsPresent = true }, { Variable = "$.intent_create", BooleanEquals = true }, { Variable = "$.action", StringEquals = "apply" }], Next = "CreateIntent" },
          { And = [{ Variable = "$.intent_create", IsPresent = true }, { Variable = "$.intent_create", BooleanEquals = true }, { Variable = "$.action", StringEquals = "destroy" }], Next = "CreateIntent" },
          { Variable = "$.action", StringEquals = "plan", Next = "ValidateAndResolve" },
          { Variable = "$.action", StringEquals = "plan_destroy", Next = "ValidateAndResolve" },
          { Variable = "$.action", StringEquals = "drift", Next = "ValidateAndResolve" },
          { Variable = "$.action", StringEquals = "report", Next = "ValidateAndResolve" },
        ]
        Default = "FailRouteAction"
      }
      CreateIntent = {
        Type     = "Task"
        Resource = local.lambda_arns["intent-create"]
        Catch    = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailCreateIntent" }]
        Next     = "RouteAfterIntent"
      }
      RouteAfterIntent = {
        Type = "Choice"
        Choices = [{
          And = [
            { Variable = "$.intent_failed", IsPresent = true },
            { Variable = "$.intent_failed", BooleanEquals = true },
          ]
          Next = "IntentFailed"
        }]
        Default = "Done"
      }
      IntentFailed = {
        Type  = "Fail"
        Error = "IntentFailed"
        Cause = "apply/destroy intent gate failed"
      }
      ValidateAndResolve = {
        Type     = "Task"
        Resource = local.lambda_arns["validate-and-resolve"]
        Catch = [
          { ErrorEquals = ["ConfigResolutionError"], ResultPath = null, Next = "NormalizeConfigError" },
          { ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailValidateAndResolve" },
        ]
        Next = "RenderPlaceholder"
      }
      # Post-resolve placeholder render failures are best-effort; execution continues to RunFolders.
      RenderPlaceholder = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          placeholder             = true
          "webhook_info.$"        = "$.webhook_info"
          "settings.$"            = "$.settings"
          "action.$"              = "$.action"
          "run_id.$"              = "$.run_id"
          "notification_target.$" = "$.notification_target"
          "map_items.$"           = "$.map_items"
          "skipped.$"             = "$.skipped"
          outcomes                = []
        }
        ResultPath = null
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "NextStep" }]
        Next       = "NextStep"
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
        Next = "RenderPR"
      }
      NextStep = {
        Type = "Choice"
        Choices = [
          { And = [{ Variable = "$.step_index", NumericLessThanPath = "$.step_count" }, { Variable = "$.current_step_items", IsPresent = true }], Next = "RunStepFolders" },
          { Variable = "$.current_step_items", IsPresent = false, Next = "RunFolders" },
        ]
        Default = "RenderPR"
      }
      RunFolders = {
        Type           = "Map"
        ItemsPath      = "$.map_items"
        MaxConcurrency = var.run_folder_max_concurrency
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
          "folder_config.$"              = "$$.Map.Item.Value.c"
          "execution_id.$"               = "$$.Map.Item.Value.e"
          "upstream_urls.$"              = "$.map_shared.upstream_urls"
          "repo_name.$"                  = "$.map_shared.repo_name"
          "git_url.$"                    = "$.map_shared.git_url"
          "commit_hash.$"                = "$.map_shared.commit_hash"
          "ssm_openci_tf_github_token.$" = "$.map_shared.ssm_openci_tf_github_token"
          "ssm_infracost_api_key.$"      = "$.map_shared.ssm_infracost_api_key"
        }
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailRunFolders" }]
        Iterator = {
          StartAt = "RunFolder"
          States = {
            RunFolder = {
              Type       = "Task"
              Resource   = "arn:aws:states:::states:startExecution.sync:2"
              Parameters = { "StateMachineArn" = var.run_folder_state_machine_arn, "Input.$" = "$" }
              ResultPath = "$.child_execution"
              Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "NormalizeFolderOutcome" }]
              Next       = "NormalizeFolderOutcome"
            }
            NormalizeFolderOutcome = {
              Type     = "Task"
              Resource = local.lambda_arns["render-pr"]
              Parameters = {
                normalize_folder_outcome = true
                "state.$"                = "$"
              }
              End = true
            }
          }
        }
        Next = "RenderPR"
      }
      RunStepFolders = {
        Type           = "Map"
        ItemsPath      = "$.current_step_items"
        MaxConcurrency = var.run_folder_max_concurrency
        ResultPath     = "$.step_outcomes"
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
          "folder_config.$"              = "$$.Map.Item.Value.c"
          "execution_id.$"               = "$$.Map.Item.Value.e"
          "upstream_urls.$"              = "$.map_shared.upstream_urls"
          "repo_name.$"                  = "$.map_shared.repo_name"
          "git_url.$"                    = "$.map_shared.git_url"
          "commit_hash.$"                = "$.map_shared.commit_hash"
          "ssm_openci_tf_github_token.$" = "$.map_shared.ssm_openci_tf_github_token"
          "ssm_infracost_api_key.$"      = "$.map_shared.ssm_infracost_api_key"
        }
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailRunStepFolders" }]
        Iterator = {
          StartAt = "RunStepFolder"
          States = {
            RunStepFolder = {
              Type       = "Task"
              Resource   = "arn:aws:states:::states:startExecution.sync:2"
              Parameters = { "StateMachineArn" = var.run_folder_state_machine_arn, "Input.$" = "$" }
              ResultPath = "$.child_execution"
              Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "NormalizeStepFolderOutcome" }]
              Next       = "NormalizeStepFolderOutcome"
            }
            # This Task is the required target for both the nested-execution success
            # and Catch paths. The consumer Lambda owns bounded success, malformed,
            # and nested-failure shaping so the iterator needs no routing Pass family.
            NormalizeStepFolderOutcome = {
              Type     = "Task"
              Resource = local.lambda_arns["render-pr"]
              Parameters = {
                normalize_folder_outcome = true
                "state.$"                = "$"
              }
              End = true
            }
          }
        }
        Next = "CollectStepOutcomes"
      }
      CollectStepOutcomes = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          collect_step_outcomes = true
          "state.$"             = "$"
          "step_outcomes.$"     = "$.step_outcomes"
        }
        ResultPath = "$"
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FailCollectStepOutcomes" }]
        Next       = "AdvanceOrStop"
      }
      AdvanceOrStop = {
        Type    = "Choice"
        Choices = [{ And = [{ Variable = "$.step_failed", IsPresent = true }, { Variable = "$.step_failed", BooleanEquals = true }], Next = "RenderPR" }]
        Default = "NextStep"
      }
      RenderPR = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          "webhook_info.$"        = "$.webhook_info"
          "settings.$"            = "$.settings"
          "action.$"              = "$.action"
          "run_id.$"              = "$.run_id"
          "deadline_at.$"         = "$.deadline_at"
          "notification_target.$" = "$.notification_target"
          "steps.$"               = "$.steps"
          "step_count.$"          = "$.step_count"
          "outcomes.$"            = "$.outcomes"
          "skipped.$"             = "$.skipped"
          "no_op_reason.$"        = "$.no_op_reason"
          "execution_arn.$"       = "$$.Execution.Id"
        }
        # ResultPath = $.render_flags keeps outcomes while surfacing terminal failure.
        ResultPath = "$.render_flags"
        Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = null, Next = "FinalizeAfterRenderFailure" }]
        Next       = "RouteAfterRender"
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
      FailParseCommand = {
        # Inject only the failure marker; the full state (map_items, outcomes,
        # webhook_info) must survive so FinalizeRun can release locks.
        Type       = "Pass"
        Result     = { failed_step = "ParseCommand" }
        ResultPath = "$.pipeline_failure"
        Next       = "RenderPipelineFailure"
      }
      FailRouteAction = {
        # Inject only the failure marker; the full state (map_items, outcomes,
        # webhook_info) must survive so FinalizeRun can release locks.
        Type       = "Pass"
        Result     = { failed_step = "RouteAction" }
        ResultPath = "$.pipeline_failure"
        Next       = "RenderPipelineFailure"
      }
      FailCreateIntent = {
        # Preserve the routed action in the failure marker so one merged intent
        # task still produces action-specific operator evidence.
        Type = "Pass"
        Parameters = {
          failed_step = "CreateIntent"
          "action.$"  = "$.action"
        }
        ResultPath = "$.pipeline_failure"
        Next       = "RenderPipelineFailure"
      }
      FailValidateAndResolve = {
        # Inject only the failure marker; the full state (map_items, outcomes,
        # webhook_info) must survive so FinalizeRun can release locks.
        Type       = "Pass"
        Result     = { failed_step = "ValidateAndResolve" }
        ResultPath = "$.pipeline_failure"
        Next       = "RenderPipelineFailure"
      }
      FailRunFolders = {
        # Inject only the failure marker; the full state (map_items, outcomes,
        # webhook_info) must survive so FinalizeRun can release locks.
        Type       = "Pass"
        Result     = { failed_step = "RunFolders" }
        ResultPath = "$.pipeline_failure"
        Next       = "RenderPipelineFailure"
      }
      FailRunStepFolders = {
        # Inject only the failure marker; the full state (map_items, outcomes,
        # webhook_info) must survive so FinalizeRun can release locks.
        Type       = "Pass"
        Result     = { failed_step = "RunStepFolders" }
        ResultPath = "$.pipeline_failure"
        Next       = "RenderPipelineFailure"
      }
      FailCollectStepOutcomes = {
        # Inject only the failure marker; the full state (map_items, outcomes,
        # webhook_info) must survive so FinalizeRun can release locks.
        Type       = "Pass"
        Result     = { failed_step = "CollectStepOutcomes" }
        ResultPath = "$.pipeline_failure"
        Next       = "RenderPipelineFailure"
      }
      RenderPipelineFailure = {
        Type     = "Task"
        Resource = local.lambda_arns["render-pr"]
        Parameters = {
          "pipeline_failure.$"    = "$.pipeline_failure"
          "webhook_info.$"        = "$.webhook_info"
          "settings.$"            = "$.settings"
          "run_id.$"              = "$.run_id"
          "notification_target.$" = "$.notification_target"
          "execution_arn.$"       = "$$.Execution.Id"
        }
        # ResultPath = null keeps the full state so FinalizeRun can still release
        # folder locks and persist outcomes (live bug: state was replaced with
        # {failed_step,...}, leaving locks held after mutation failures).
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
        Cause = "openci-tf outer pipeline failed"
      }
      FinalizeRunFailed = {
        Type  = "Fail"
        Error = "FinalizeRunFailed"
        Cause = "registry finalization failed"
      }
      Done = { Type = "Pass", End = true }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_function.arn}:*"
    level                  = "ERROR"
    include_execution_data = true
  }
  tags = merge(var.tags, { Name = var.project_name })
}
