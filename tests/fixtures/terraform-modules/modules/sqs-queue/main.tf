locals {
  tags = merge(var.tags, { Module = "sqs-queue" })
}

resource "aws_sqs_queue" "this" {
  name                      = var.name
  message_retention_seconds = var.message_retention_seconds

  tags = local.tags
}
