resource "aws_cloudwatch_event_rule" "apply_execution_failures" {
  name        = "${var.project_name}-apply-execution-failures"
  description = "Finalize registry runs after uncatchable apply Step Functions failures"

  event_pattern = jsonencode({
    source        = ["aws.states"]
    "detail-type" = ["Step Functions Execution Status Change"]
    detail = {
      status          = ["FAILED", "TIMED_OUT", "ABORTED"]
      stateMachineArn = [aws_sfn_state_machine.openci_tf_apply.arn]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "destroy_execution_failures" {
  name        = "${var.project_name}-destroy-execution-failures"
  description = "Finalize registry runs after uncatchable destroy Step Functions failures"

  event_pattern = jsonencode({
    source        = ["aws.states"]
    "detail-type" = ["Step Functions Execution Status Change"]
    detail = {
      status          = ["FAILED", "TIMED_OUT", "ABORTED"]
      stateMachineArn = [aws_sfn_state_machine.openci_tf_destroy.arn]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "apply_failure_finalizer" {
  rule = aws_cloudwatch_event_rule.apply_execution_failures.name
  arn  = aws_lambda_function.functions["finalize-run"].arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}

resource "aws_cloudwatch_event_target" "destroy_failure_finalizer" {
  rule = aws_cloudwatch_event_rule.destroy_execution_failures.name
  arn  = aws_lambda_function.functions["finalize-run"].arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}

resource "aws_lambda_permission" "allow_apply_failure_events" {
  statement_id  = "AllowApplyExecutionFailureEvents"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["finalize-run"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.apply_execution_failures.arn
}

resource "aws_lambda_permission" "allow_destroy_failure_events" {
  statement_id  = "AllowDestroyExecutionFailureEvents"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["finalize-run"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.destroy_execution_failures.arn
}
