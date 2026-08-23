# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
resource "aws_cloudwatch_log_group" "step_function" {
  name              = "/aws/vendedlogs/states/${local.state_machine_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_cloudwatch_metric_alarm" "state_machine_failures" {
  alarm_name          = "${local.state_machine_name}-execution-failures"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.this.arn }
  alarm_actions       = ["arn:aws:sns:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${var.project_name}-alarms"]
}
