output "executor_readonly_role_arn" {
  description = "ARN of the executor-readonly role"
  value       = aws_iam_role.executor_readonly.arn
}

output "executor_readonly_role_name" {
  description = "Name of the executor-readonly role"
  value       = aws_iam_role.executor_readonly.name
}

output "executor_readonly_permissions_boundary_policy_arn" {
  description = "ARN of the executor-readonly permissions boundary policy"
  value       = aws_iam_policy.executor_readonly_permissions_boundary.arn
}

output "executor_readonly_permissions_boundary_policy_name" {
  description = "Name of the executor-readonly permissions boundary policy"
  value       = aws_iam_policy.executor_readonly_permissions_boundary.name
}
