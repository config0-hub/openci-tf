variable "name" {
  type        = string
  description = "Name prefix for VPC resources."
}

variable "vpc_cidr_block" {
  type        = string
  description = "CIDR block for the VPC."
}

variable "public_subnet_cidr_block" {
  type        = string
  description = "CIDR block for the single public subnet."
}

variable "availability_zone" {
  type        = string
  description = "Availability zone for the public subnet."
}

variable "tags" {
  type        = map(string)
  description = "Tags applied to all taggable resources."
  default     = {}
}
