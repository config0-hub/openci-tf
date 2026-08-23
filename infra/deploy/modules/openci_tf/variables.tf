# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
variable "project_name" {
  description = "Project name prefix for resource naming"
  type        = string
  default     = "openci-tf"
}

variable "ecr_image_uri" {
  description = "ECR image URI for Lambda functions (repo_url:tag)"
  type        = string
}

variable "engine_init_lambda_name" {
  description = "Function name of the engine init_job Lambda"
  type        = string
  default     = ""
}

variable "engine_init_lambda_arn" {
  description = "ARN of the engine init_job Lambda (for invoke permission)"
  type        = string
  default     = ""
}

variable "assume_role_arns" {
  description = "IAM role ARNs that openci-tf Lambdas can assume for cross-account access"
  type        = list(string)
  default     = []
}
variable "tmp_bucket_name" { type = string }
variable "tmp_bucket_arn" { type = string }
variable "kms_key_arn" { type = string }
variable "done_bucket_arn" { type = string }
variable "run_folder_state_machine_arn" { type = string }
variable "run_folder_apply_state_machine_arn" { type = string }
variable "run_folder_destroy_state_machine_arn" { type = string }
variable "log_retention_days" {
  description = "CloudWatch log retention for all openci-tf workloads"
  type        = number
  default     = 14
}

variable "run_history_retention_days" {
  description = "Default TTL for run-registry history items"
  type        = number
  default     = 90
}

variable "done_bucket_name" {
  type = string
}

variable "package_bucket_name" {
  type = string
}

variable "api_caller_policy_json" {
  description = "Map of IAM principal patterns to allowed API operations"
  type = map(object({
    trigger_ids      = list(string)
    actions          = list(string)
    artifact_classes = list(string)
    binary_plan      = bool
    read_classes     = optional(list(string), [])
  }))
  default = {}
}

variable "tmp_lifecycle_days" {
  type    = number
  default = 3

  validation {
    condition     = var.tmp_lifecycle_days > 0 && var.tmp_lifecycle_days <= 3660 && floor(var.tmp_lifecycle_days) == var.tmp_lifecycle_days
    error_message = "tmp_lifecycle_days must be a positive integer between 1 and 3660."
  }
}

variable "package_lifecycle_days" {
  type    = number
  default = 30

  validation {
    condition     = var.package_lifecycle_days > 0 && var.package_lifecycle_days <= 3660 && floor(var.package_lifecycle_days) == var.package_lifecycle_days
    error_message = "package_lifecycle_days must be a positive integer between 1 and 3660."
  }
}

variable "done_lifecycle_days" {
  type    = number
  default = 365

  validation {
    condition     = var.done_lifecycle_days > 0 && var.done_lifecycle_days <= 3660 && floor(var.done_lifecycle_days) == var.done_lifecycle_days
    error_message = "done_lifecycle_days must be a positive integer between 1 and 3660."
  }
}

variable "plan_retention_days" {
  type    = number
  default = 2

  validation {
    condition     = var.plan_retention_days > 0 && var.plan_retention_days <= 3660 && floor(var.plan_retention_days) == var.plan_retention_days
    error_message = "plan_retention_days must be a positive integer between 1 and 3660."
  }
}

variable "aws_console_start_url" {
  description = "Optional IAM Identity Center start URL used to build account-aware AWS Console shortcut links"
  type        = string
  default     = ""
}

variable "aws_console_role_name" {
  description = "Optional IAM Identity Center permission-set role name used with aws_console_start_url for AWS Console shortcut links"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
