locals {
  tags = merge(var.tags, { Module = "cloudwatch-log-group" })
}

resource "aws_cloudwatch_log_group" "this" {
  name              = var.name
  retention_in_days = var.retention_in_days

  tags = local.tags
}
