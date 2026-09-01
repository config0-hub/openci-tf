# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
variable "role_prefix" {
  description = "Prefix for IAM role names (e.g. 'openci-tf')"
  type        = string
  # ref 4353245 - openci-tf remote executor consistency naming
  default = "openci-tf"
}

variable "target_account_ids" {
  description = "Registered target AWS account IDs whose executor-readonly and executor-poweruser roles may be assumed"
  type        = set(string)

  validation {
    condition     = alltrue([for account_id in var.target_account_ids : can(regex("^[0-9]{12}$", account_id))])
    error_message = "target_account_ids must contain only 12-digit AWS account IDs."
  }
}

variable "target_account_wildcard" {
  description = "When true, hub-lambda-exec may assume <role_prefix>-executor-* roles in any account (arn:aws:iam::*:role/<role_prefix>-executor-*) instead of only the enumerated target_account_ids; the target role's own trust policy remains the gate"
  type        = bool
  default     = false
}

variable "state_bucket_arn" {
  description = "ARN of the main account Terraform state bucket used by same-account execution"
  type        = string
}


variable "enable_apply" {
  description = "Legacy IAM migration only: when true, attach PowerUserAccess to executor-local; runtime intent gating uses DynamoDB enable_apply"
  type        = bool
  default     = false
}
