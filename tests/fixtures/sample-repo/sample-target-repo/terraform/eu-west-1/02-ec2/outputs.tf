output "region" {
  description = "AWS region for this workload root."
  value       = local.aws_region
}

output "probe_instance_id" {
  description = "SSM-managed probe instance identifier."
  value       = aws_instance.probe.id
}

output "sns_topic_arn" {
  description = "Tracer SNS topic ARN."
  value       = aws_sns_topic.events.arn
}

output "sqs_queue_url" {
  description = "Primary SQS queue URL."
  value       = aws_sqs_queue.main.url
}

output "sqs_dlq_url" {
  description = "Dead-letter queue URL."
  value       = aws_sqs_queue.dlq.url
}

output "dynamodb_table_name" {
  description = "Tracer DynamoDB table name."
  value       = aws_dynamodb_table.tracer.name
}
