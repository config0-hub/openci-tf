# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "repository_url" {
  description = "ECR repository URL"
  value       = aws_ecr_repository.repo.repository_url
}

output "repository_arn" {
  description = "ECR repository ARN"
  value       = aws_ecr_repository.repo.arn
}
