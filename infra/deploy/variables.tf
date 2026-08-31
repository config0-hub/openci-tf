# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  # ref 4353245 - openci-tf remote executor consistency naming
  default = "openci-tf"
}

variable "image_tag" {
  description = "Docker image tag read from the checked-in IMAGE_VERSION file by installation tooling"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "install_mode" {
  description = "Install flavor: 'standalone' (own state bucket + enumerated target accounts) or 'config0-addon' (tenant-provided state bucket, executor-* assume-role trust by name pattern). Both lock via the S3 native lock file; no DynamoDB lock table exists."
  type        = string
  default     = "standalone"

  validation {
    condition     = contains(["standalone", "config0-addon"], var.install_mode)
    error_message = "install_mode must be 'standalone' or 'config0-addon'."
  }
}

variable "state_bucket_name" {
  description = "Terraform state bucket used by executor session policies and same-account execution; empty string derives the standalone default <project_name>-state-<account_id>. config0-addon installs must pass the tenant's existing state bucket."
  type        = string
  default     = ""
}

variable "engine_name" {
  description = "Name prefix of the AWS execution engine deployment to reuse (<engine_name>-init-job, <engine_name>-codebuild, <engine_name>-worker, <engine_name>-finalizer); empty string derives the standalone default project_name"
  type        = string
  default     = ""
}

variable "target_account_ids" {
  description = "Registered target AWS account IDs whose executor-remote roles the hub may assume"
  type        = set(string)
}

variable "run_history_retention_days" {
  description = "Default TTL for run-registry history items"
  type        = number
  default     = 90

  validation {
    condition     = var.run_history_retention_days > 0 && var.run_history_retention_days <= 3650
    error_message = "run_history_retention_days must be between 1 and 3650."
  }
}

variable "run_folder_max_concurrency" {
  description = "MaxConcurrency for read-lane folder Map states in the outer state machine"
  type        = number
  default     = 40

  validation {
    condition     = var.run_folder_max_concurrency >= 1 && var.run_folder_max_concurrency <= 40
    error_message = "run_folder_max_concurrency must be between 1 and 40."
  }
}

variable "tmp_lifecycle_days" {
  description = "Foundation tmp bucket lifecycle retention in days"
  type        = number
  default     = 3

  validation {
    condition     = var.tmp_lifecycle_days > 0 && var.tmp_lifecycle_days <= 3660 && floor(var.tmp_lifecycle_days) == var.tmp_lifecycle_days
    error_message = "tmp_lifecycle_days must be a positive integer between 1 and 3660."
  }
}

variable "package_lifecycle_days" {
  description = "Foundation package bucket lifecycle retention in days"
  type        = number
  default     = 30

  validation {
    condition     = var.package_lifecycle_days > 0 && var.package_lifecycle_days <= 3660 && floor(var.package_lifecycle_days) == var.package_lifecycle_days
    error_message = "package_lifecycle_days must be a positive integer between 1 and 3660."
  }
}

variable "done_lifecycle_days" {
  description = "Foundation done bucket lifecycle retention in days"
  type        = number
  default     = 365

  validation {
    condition     = var.done_lifecycle_days > 0 && var.done_lifecycle_days <= 3660 && floor(var.done_lifecycle_days) == var.done_lifecycle_days
    error_message = "done_lifecycle_days must be a positive integer between 1 and 3660."
  }
}

variable "plan_retention_days" {
  description = "Foundation binary plan lifecycle retention in days"
  type        = number
  default     = 1

  validation {
    condition     = var.plan_retention_days > 0 && var.plan_retention_days <= 3660 && floor(var.plan_retention_days) == var.plan_retention_days
    error_message = "plan_retention_days must be a positive integer between 1 and 3660."
  }
}


variable "api_caller_policy_json" {
  description = "Map of IAM role ARN patterns to allowed API operations for core routes"
  type = map(object({
    trigger_ids      = list(string)
    actions          = list(string)
    artifact_classes = list(string)
    binary_plan      = bool
    read_classes     = optional(list(string), [])
  }))
  default = {}
}

variable "enable_apply" {
  description = "Legacy IAM migration only: when true, attach PowerUserAccess to executor-local; runtime intent gating uses DynamoDB enable_apply"
  type        = bool
  default     = false
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
