output "region" {
  description = "AWS region for this VPC root."
  value       = local.aws_region
}

output "vpc_id" {
  description = "Disposable tracer VPC identifier."
  value       = aws_vpc.main.id
}

output "vpc_cidr" {
  description = "VPC CIDR block."
  value       = local.vpc_cidr
}

output "public_subnet_id" {
  description = "Public subnet identifier for workload roots."
  value       = aws_subnet.public.id
}
