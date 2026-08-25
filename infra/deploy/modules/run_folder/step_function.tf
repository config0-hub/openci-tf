# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
resource "aws_sfn_state_machine" "this" {
  name     = local.state_machine_name
  role_arn = aws_iam_role.sfn.arn
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_function.arn}:*"
    level                  = "ERROR"
    include_execution_data = true
  }
  definition = jsonencode({
    StartAt = "ValidateAction"
    States = merge(
      {
        ValidateAction = {
          Type = "Choice"
          Choices = [for action in local.allowed_actions : {
            Variable = "$.action", StringEquals = action, Next = "PrepareAndSubmit"
          }]
          Default = "WriteFailureManifest"
        }
        PrepareAndSubmit = {
          Type       = "Task"
          Resource   = aws_lambda_function.this["prepare-and-submit"].arn
          ResultPath = "$.result"
          Catch = [
            { ErrorEquals = ["CredentialExpiredError"], ResultPath = "$.error", Next = "RouteProbeOutcome" },
            { ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "WriteFailureManifest" },
          ]
          Next = "ProbeDone"
        }
        ProbeDone = {
          Type       = "Task"
          Resource   = aws_lambda_function.this["poll-done"].arn
          ResultPath = "$.probe"
          Catch = [
            { ErrorEquals = ["CredentialExpiredError"], ResultPath = "$.error", Next = "RouteProbeOutcome" },
            { ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "WriteFailureManifest" },
          ]
          Next = "RouteProbeOutcome"
        }
        RouteProbeOutcome = {
          Type = "Choice"
          Choices = [
            {
              And = [
                { Variable = "$.error.Error", IsPresent = true },
                { Variable = "$.error.Error", StringEquals = "CredentialExpiredError" },
                { Variable = "$.attempt", IsPresent = true },
                { Variable = "$.attempt", NumericLessThan = 1 },
              ]
              Next = "BookkeepCredentialRetry"
            },
            {
              And = [
                { Variable = "$.error.Error", IsPresent = true },
              ]
              Next = "WriteFailureManifest"
            },
            {
              And = [
                { Variable = "$.probe.probe_status", IsPresent = true },
                { Variable = "$.probe.probe_status", StringEquals = "pending" },
              ]
              Next = "WaitBeforeProbe"
            },
            {
              And = [
                { Variable = "$.probe.succeeded", IsPresent = true },
                { Variable = "$.probe.succeeded", BooleanEquals = false },
                { Variable = "$.probe.credential_expired", IsPresent = true },
                { Variable = "$.probe.credential_expired", BooleanEquals = true },
                { Variable = "$.probe.attempt", IsPresent = true },
                { Variable = "$.probe.attempt", NumericLessThan = 1 },
              ]
              Next = "BookkeepCredentialRetry"
            },
            {
              And = [
                { Variable = "$.probe.probe_status", IsPresent = true },
                { Variable = "$.probe.probe_status", StringEquals = "complete" },
              ]
              Next = local.mutation_lane ? "CollectMutation" : "Collect"
            },
            {
              And = [
                { Variable = "$.probe.probe_status", IsPresent = true },
                { Variable = "$.probe.probe_status", StringEquals = "terminal" },
              ]
              Next = local.mutation_lane ? "CollectMutation" : "Collect"
            },
            {
              And = [
                { Variable = "$.probe.probe_status", IsPresent = true },
                { Variable = "$.probe.probe_status", StringEquals = "expired" },
              ]
              Next = "WriteFailureManifest"
            },
          ]
          Default = "WriteFailureManifest"
        }
        WaitBeforeProbe = {
          Type    = "Wait"
          Seconds = 30
          Next    = "ProbeDone"
        }
        BookkeepCredentialRetry = {
          Type     = "Task"
          Resource = aws_lambda_function.this["persist-retry-attempt"].arn
          Parameters = {
            "event.$"                = "$"
            "execution_started_at.$" = "$$.Execution.StartTime"
          }
          ResultPath = "$"
          Retry      = [{ ErrorEquals = ["States.ALL"], IntervalSeconds = 1, MaxAttempts = 3, BackoffRate = 2.0 }]
          Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "WriteFailureManifestFailed" }]
          Next       = "PrepareAndSubmit"
        }
        WriteFailureManifest = {
          Type     = "Task"
          Resource = aws_lambda_function.this["write-failure-manifest"].arn
          Parameters = {
            "event.$"                = "$"
            "execution_started_at.$" = "$$.Execution.StartTime"
          }
          Retry      = [{ ErrorEquals = ["States.ALL"], IntervalSeconds = 1, MaxAttempts = 3, BackoffRate = 2.0 }]
          Catch      = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "WriteFailureManifestFailed" }]
          ResultPath = null
          Next       = "FolderExecutionFailed"
        }
        FolderExecutionFailed = {
          Type  = "Fail"
          Error = "FolderExecutionFailed"
          Cause = "folder execution failed after failure manifest persistence"
        }
        WriteFailureManifestFailed = {
          Type  = "Fail"
          Error = "WriteFailureManifestFailed"
          Cause = "failure manifest writer exhausted retries"
        }
      },
      merge([for lane in [var.lane] : {
        CollectMutation = {
          Type     = "Task"
          Resource = aws_lambda_function.this["collect"].arn
          Parameters = {
            "exec_id.$"            = "$.probe.exec_id"
            "attempt.$"            = "$.probe.attempt"
            "succeeded.$"          = "$.probe.succeeded"
            "credential_expired.$" = "$.probe.credential_expired"
            "steps.$"              = "$.probe.steps"
            "error.$"              = "$.probe.error"
            "pointers.$"           = "$.probe.pointers"
            "action.$"             = "$.action"
            "repo_name.$"          = "$.repo_name"
            "commit_hash.$"        = "$.commit_hash"
            "account_id.$"         = "$.account_id"
            "folder.$"             = "$.folder"
            "run_id.$"             = "$.run_id"
            "deadline_at.$"        = "$.deadline_at"
            "submitted_at.$"       = "$.probe.submitted_at"
            "source_plan_run_id.$" = "$.source_plan_run_id"
            "step_index.$"         = "$.step_index"
          }
          Retry = [{ ErrorEquals = ["States.ALL"], IntervalSeconds = 1, MaxAttempts = 3, BackoffRate = 2.0 }]
          Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "WriteFailureManifest" }]
          End   = true
        }
      } if lane != "read"]...),
      merge([for lane in [var.lane] : {
        Collect = {
          Type     = "Task"
          Resource = aws_lambda_function.this["collect"].arn
          Parameters = {
            "exec_id.$"            = "$.probe.exec_id"
            "attempt.$"            = "$.probe.attempt"
            "succeeded.$"          = "$.probe.succeeded"
            "credential_expired.$" = "$.probe.credential_expired"
            "steps.$"              = "$.probe.steps"
            "error.$"              = "$.probe.error"
            "pointers.$"           = "$.probe.pointers"
            "action.$"             = "$.action"
            "repo_name.$"          = "$.repo_name"
            "commit_hash.$"        = "$.commit_hash"
            "account_id.$"         = "$.account_id"
            "folder.$"             = "$.folder"
            "run_id.$"             = "$.run_id"
            "deadline_at.$"        = "$.deadline_at"
            "submitted_at.$"       = "$.probe.submitted_at"
            "step_index.$"         = "$.step_index"
          }
          Retry = [{ ErrorEquals = ["States.ALL"], IntervalSeconds = 1, MaxAttempts = 3, BackoffRate = 2.0 }]
          Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "WriteFailureManifest" }]
          End   = true
        }
      } if lane == "read"]...)
    )
  })
}
