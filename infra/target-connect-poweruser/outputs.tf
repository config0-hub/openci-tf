# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "executor_poweruser_role_arn" {
  description = "ARN of the executor-poweruser role created in the target account"
  value       = module.executor_poweruser.executor_poweruser_role_arn
}
