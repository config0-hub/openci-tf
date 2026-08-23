variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "name_prefix" {
  type = string
  # ref 4353245 - openci-tf remote executor consistency naming
  default = "openci-tf"
}
# Bucket names are DELIBERATELY not overridable: downstream stacks (deploy,
# engine) discover them by the deterministic <prefix>-{tmp,package,done}-<acct>
# convention via data sources. An override here would break those lookups.
variable "tmp_size_alarm_bytes" {
  type    = number
  default = 1073741824
}
variable "package_size_alarm_bytes" {
  type    = number
  default = 10737418240
}
variable "done_size_alarm_bytes" {
  type    = number
  default = 10737418240
}
variable "done_expiration_days" {
  type    = number
  default = 365

  validation {
    condition     = var.done_expiration_days > 0 && var.done_expiration_days <= 3660 && floor(var.done_expiration_days) == var.done_expiration_days
    error_message = "done_expiration_days must be a positive integer between 1 and 3660."
  }
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
variable "plan_retention_days" {
  type    = number
  default = 1

  validation {
    condition     = var.plan_retention_days > 0 && var.plan_retention_days <= 3660 && floor(var.plan_retention_days) == var.plan_retention_days
    error_message = "plan_retention_days must be a positive integer between 1 and 3660."
  }
}
