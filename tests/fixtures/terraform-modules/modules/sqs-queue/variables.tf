variable "name" {
  type        = string
  description = "SQS queue name."
}

variable "message_retention_seconds" {
  type        = number
  description = "How long SQS retains messages."
  default     = 345600
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the queue."
  default     = {}
}
