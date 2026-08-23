output "queue_name" {
  description = "SQS queue name."
  value       = aws_sqs_queue.this.name
}

output "queue_url" {
  description = "SQS queue URL."
  value       = aws_sqs_queue.this.url
}

output "queue_arn" {
  description = "SQS queue ARN."
  value       = aws_sqs_queue.this.arn
}
