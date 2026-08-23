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

variable "state_bucket_arn" {
  description = "ARN of the main account Terraform state bucket used by same-account execution"
  type        = string
}

variable "lock_table_arn" {
  description = "ARN of the main account Terraform state lock table used by same-account execution"
  type        = string
}

variable "enable_apply" {
  description = "Legacy IAM migration only: when true, attach PowerUserAccess to executor-local; runtime intent gating uses DynamoDB enable_apply"
  type        = bool
  default     = false
}

variable "provision_legacy_executor_local" {
  description = "When true, retain the legacy executor-local IAM role for pre-split upgrades; set false via install SSM to retire durably"
  type        = bool
  default     = true
}
