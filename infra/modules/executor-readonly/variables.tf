# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
variable "role_prefix" {
  description = "Prefix for IAM role names (e.g. 'openci-tf')"
  type        = string
  default     = "openci-tf"
}

variable "hub_lambda_exec_role_arn" {
  description = "ARN of the hub-lambda-exec role that can assume this role"
  type        = string
}

variable "state_bucket_arn" {
  description = "ARN of the target account Terraform state bucket"
  type        = string
}

