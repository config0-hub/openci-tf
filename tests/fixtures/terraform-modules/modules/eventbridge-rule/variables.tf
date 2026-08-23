variable "name" {
  type        = string
  description = "EventBridge rule name."
}

variable "description" {
  type        = string
  description = "EventBridge rule description."
  default     = "Disabled test schedule rule."
}

variable "schedule_expression" {
  type        = string
  description = "Schedule expression for the rule."
  default     = "rate(1 day)"
}

variable "enabled" {
  type        = bool
  description = "Whether the schedule rule is enabled."
  default     = false
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the rule."
  default     = {}
}
