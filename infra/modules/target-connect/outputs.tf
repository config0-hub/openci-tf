output "executor_remote_role_arn" {
  description = "ARN of the legacy executor-remote role; empty when retired"
  value       = length(aws_iam_role.executor_remote) > 0 ? aws_iam_role.executor_remote[0].arn : ""
}
