variable "project_name" {
  description = "Project name prefix for resource naming"
  type        = string
  default     = "openci-tf"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the console Lambda"
  type        = number
  default     = 14
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
