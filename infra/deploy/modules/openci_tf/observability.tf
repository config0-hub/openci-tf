# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
resource "aws_sqs_queue" "webhook_dlq" {
  name = "${var.project_name}-webhook-dlq"
  tags = var.tags
}

resource "aws_cloudwatch_log_group" "step_function" {
  name              = "/aws/vendedlogs/states/${var.project_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_sns_topic" "alarms" {
  name = "${var.project_name}-alarms"
  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = local.lambdas
  alarm_name          = "${var.project_name}-${each.key}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions          = { FunctionName = aws_lambda_function.functions[each.key].function_name }
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "webhook_dlq" {
  alarm_name          = "${var.project_name}-webhook-dlq-messages"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions          = { QueueName = aws_sqs_queue.webhook_dlq.name }
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "state_machine_failures" {
  alarm_name          = "${var.project_name}-execution-failures"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.openci_tf.arn }
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_dashboard" "openci_tf" {
  dashboard_name = var.project_name
  dashboard_body = jsonencode({ widgets = [
    { type = "metric", properties = { title = "Runs", view = "timeSeries", region = data.aws_region.current.name, metrics = [["AWS/States", "ExecutionsStarted", "StateMachineArn", aws_sfn_state_machine.openci_tf.arn]] } },
    { type = "metric", properties = { title = "Failures", view = "timeSeries", region = data.aws_region.current.name, metrics = [["AWS/States", "ExecutionsFailed", "StateMachineArn", aws_sfn_state_machine.openci_tf.arn]] } },
    { type = "metric", properties = { title = "Durations", view = "timeSeries", region = data.aws_region.current.name, metrics = [["AWS/States", "ExecutionTime", "StateMachineArn", aws_sfn_state_machine.openci_tf.arn]] } }
  ] })
}
