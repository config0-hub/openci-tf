variable "name" {
  type        = string
  description = "DynamoDB table name."
}

variable "hash_key" {
  type        = string
  description = "Hash key attribute name."
  default     = "pk"
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the table."
  default     = {}
}
