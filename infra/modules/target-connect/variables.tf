# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
variable "role_prefix" {
  description = "Prefix for IAM role names (e.g. 'openci-tf')"
  type        = string
  # ref 4353245 - openci-tf remote executor consistency naming
  default = "openci-tf"
}

variable "hub_lambda_exec_role_arn" {
  description = "ARN of the hub-lambda-exec role that can assume this role"
  type        = string
}

variable "state_bucket_arn" {
  description = "ARN of the target account Terraform state bucket"
  type        = string
}

variable "lock_table_arn" {
  description = "ARN of the target account Terraform state lock table"
  type        = string
}

variable "enable_apply" {
  description = "When true, attach PowerUserAccess to executor-remote; runtime intent gating uses DynamoDB enable_apply"
  type        = bool
  default     = false
}
