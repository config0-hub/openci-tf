# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
resource "aws_cloudwatch_event_rule" "outer_execution_failures" {
  name        = "${var.project_name}-outer-execution-failures"
  description = "Finalize registry runs after uncatchable outer Step Functions failures"

  event_pattern = jsonencode({
    source        = ["aws.states"]
    "detail-type" = ["Step Functions Execution Status Change"]
    detail = {
      status          = ["FAILED", "TIMED_OUT", "ABORTED"]
      stateMachineArn = [aws_sfn_state_machine.openci_tf.arn]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "outer_failure_finalizer" {
  rule = aws_cloudwatch_event_rule.outer_execution_failures.name
  arn  = aws_lambda_function.functions["finalize-run"].arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}

resource "aws_lambda_permission" "allow_outer_failure_events" {
  statement_id  = "AllowOuterExecutionFailureEvents"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["finalize-run"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.outer_execution_failures.arn
}
