locals {
  tags = merge(var.tags, { Module = "sns-topic" })
}

resource "aws_sns_topic" "this" {
  name = var.name

  tags = local.tags
}
