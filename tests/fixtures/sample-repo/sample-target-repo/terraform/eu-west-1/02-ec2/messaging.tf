resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name_prefix}-dlq"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600

  tags = {
    Name = "${local.name_prefix}-dlq"
  }

  depends_on = [terraform_data.account_guard]
}

resource "aws_sqs_queue" "main" {
  name                    = "${local.name_prefix}-queue"
  sqs_managed_sse_enabled = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })

  tags = {
    Name = "${local.name_prefix}-queue"
  }
}

resource "aws_sns_topic" "events" {
  name              = "${local.name_prefix}-events"
  kms_master_key_id = "alias/aws/sns"

  tags = {
    Name = "${local.name_prefix}-events"
  }
}

resource "aws_sns_topic_subscription" "queue" {
  topic_arn = aws_sns_topic.events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.main.arn
}

data "aws_iam_policy_document" "queue" {
  statement {
    sid    = "AllowSNSSendMessage"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_sns_topic.events.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.id
  policy    = data.aws_iam_policy_document.queue.json
}
