# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "hub_lambda_exec_role_arn" {
  description = "ARN of the hub-lambda-exec role"
  value       = aws_iam_role.hub_lambda_exec.arn
}

output "executor_readonly_role_arn" {
  description = "ARN of the same-account executor-readonly role"
  value       = aws_iam_role.executor_readonly.arn
}

output "executor_local_role_arn" {
  description = "ARN of the same-account executor-local role"
  value       = aws_iam_role.executor_local.arn
}
