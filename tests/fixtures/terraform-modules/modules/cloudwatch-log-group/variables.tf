variable "name" {
  type        = string
  description = "CloudWatch log group name."
}

variable "retention_in_days" {
  type        = number
  description = "Log retention in days."
  default     = 1
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the log group."
  default     = {}
}
