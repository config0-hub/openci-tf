# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "openci-tf"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "hub_lambda_exec_role_arn" {
  description = "Hub account hub-lambda-exec role ARN (empty = same-account data lookup)"
  type        = string
  default     = ""
}

variable "state_bucket_arn" {
  description = "Target account Terraform state bucket ARN (empty = deterministic same-account name)"
  type        = string
  default     = ""
}

variable "enable_apply" {
  description = "Legacy IAM migration only: when true, attach PowerUserAccess to executor-remote; runtime intent gating uses DynamoDB enable_apply"
  type        = bool
  default     = false
}

variable "provision_legacy_executor_remote" {
  description = "When true, retain legacy executor-remote in target-connect state; persisted in target-account install SSM"
  type        = bool
  default     = true
}
