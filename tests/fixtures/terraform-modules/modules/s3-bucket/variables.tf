variable "name_prefix" {
  type        = string
  description = "Lowercase S3 bucket name prefix. A random suffix is appended."
}

variable "force_destroy" {
  type        = bool
  description = "Whether Terraform may delete a non-empty bucket."
  default     = true
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to the bucket."
  default     = {}
}
