output "rule_name" {
  description = "EventBridge rule name."
  value       = aws_cloudwatch_event_rule.this.name
}

output "rule_arn" {
  description = "EventBridge rule ARN."
  value       = aws_cloudwatch_event_rule.this.arn
}
