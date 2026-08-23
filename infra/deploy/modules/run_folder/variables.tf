# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
variable "project_name" { type = string }

variable "lane" {
  description = "Execution lane: read (plan/drift/report), apply, or destroy"
  type        = string
  default     = "read"

  validation {
    condition     = contains(["read", "apply", "destroy"], var.lane)
    error_message = "lane must be read, apply, or destroy"
  }
}

variable "engine_codebuild_state_machine_arn" {
  description = "Engine CodeBuild state machine ARN for mutation lanes (no init-job Lambda)"
  type        = string
  default     = ""
}
variable "engine_codebuild_project_name" {
  description = "Unmodified engine CodeBuild worker project name used for build-ID lookup"
  type        = string
  default     = ""
}
variable "ecr_image_uri" { type = string }
variable "kms_key_arn" { type = string }
variable "package_bucket_name" { type = string }
variable "package_bucket_arn" { type = string }
variable "tmp_bucket_name" { type = string }
variable "tmp_bucket_arn" { type = string }
variable "done_bucket_name" { type = string }
variable "done_bucket_arn" { type = string }
variable "engine_init_lambda_arn" { type = string }
variable "engine_init_lambda_name" { type = string }
variable "tags" { type = map(string) }
variable "log_retention_days" {
  type    = number
  default = 14
}

variable "run_history_retention_days" {
  type    = number
  default = 90
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
