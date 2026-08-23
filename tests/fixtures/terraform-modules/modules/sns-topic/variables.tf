variable "name" {
  type        = string
  description = "SNS topic name."
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the topic."
  default     = {}
}
