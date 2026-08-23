# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "executor_readonly_role_arn" {
  description = "ARN of the executor-readonly role created in the target account"
  value       = module.executor_readonly.executor_readonly_role_arn
}

output "executor_remote_role_arn" {
  description = "ARN of the legacy executor-remote role (pre-split)"
  value       = module.target_connect.executor_remote_role_arn
}
