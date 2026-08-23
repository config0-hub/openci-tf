locals {
  tags = merge(var.tags, { Module = "eventbridge-rule" })
}

resource "aws_cloudwatch_event_rule" "this" {
  name                = var.name
  description         = var.description
  schedule_expression = var.schedule_expression
  state               = var.enabled ? "ENABLED" : "DISABLED"

  tags = local.tags
}
