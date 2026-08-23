variable "project_name" {
  description = "Project name used for ECR repository name"
  type        = string
  default     = "openci-tf"
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
