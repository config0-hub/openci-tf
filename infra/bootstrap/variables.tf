variable "project_name" {
  description = "Project name prefix for resource naming"
  type        = string
  # ref 4353245 - openci-tf remote executor consistency naming
  default = "openci-tf"
}

variable "state_bucket_name" {
  description = "Name of the S3 bucket for Terraform state storage (empty = deterministic openci-tf-state-<account-id>)"
  type        = string
  default     = ""
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}
