# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "bucket_name" {
  description = "Name of the state bucket"
  value       = aws_s3_bucket.state.id
}

output "bucket_arn" {
  description = "ARN of the state bucket"
  value       = aws_s3_bucket.state.arn
}
