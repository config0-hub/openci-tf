# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "ecr_repo_url" {
  description = "ECR repository URL"
  value       = module.ecr.repository_url
}

output "webhook_url" {
  description = "Webhook URL: POST {url}/{trigger_id}"
  value       = module.openci_tf.webhook_url
}

output "api_url" {
  description = "AWS IAM core API base URL"
  value       = module.openci_tf.api_url
}

output "run_registry_table_name" {
  description = "DynamoDB run registry table"
  value       = module.openci_tf.run_registry_table_name
}

output "state_machine_arn" {
  description = "Step Function state machine ARN"
  value       = module.openci_tf.state_machine_arn
}

output "settings_table_name" {
  description = "DynamoDB settings table name"
  value       = module.openci_tf.settings_table_name
}

output "hub_lambda_exec_role_arn" {
  description = "Hub Lambda exec role ARN (assumes executor-* roles)"
  value       = module.hub_setup.hub_lambda_exec_role_arn
}

output "executor_local_role_arn" {
  description = "Executor-local role ARN (same-account execution)"
  value       = module.hub_setup.executor_local_role_arn
}

output "executor_remote_role_name" {
  description = "Derived target-account executor role name"
  value       = local.executor_remote_role_name
}

output "alarm_topic_arn" { value = module.openci_tf.alarm_topic_arn }
