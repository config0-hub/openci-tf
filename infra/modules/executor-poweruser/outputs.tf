output "executor_poweruser_role_arn" {
  description = "ARN of the executor-poweruser role"
  value       = aws_iam_role.executor_poweruser.arn
}

output "executor_poweruser_role_name" {
  description = "Name of the executor-poweruser role"
  value       = aws_iam_role.executor_poweruser.name
}
