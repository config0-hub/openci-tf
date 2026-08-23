# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "executor_poweruser_role_arn" {
  description = "ARN of the executor-poweruser role"
  value       = aws_iam_role.executor_poweruser.arn
}

output "executor_poweruser_role_name" {
  description = "Name of the executor-poweruser role"
  value       = aws_iam_role.executor_poweruser.name
}
