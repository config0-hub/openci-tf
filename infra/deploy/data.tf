# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# Cross-stack wiring via data-source lookups on deterministic names.
# Foundation, engine, and bootstrap resources are discovered — not hand-threaded.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # ref 4353245 - openci-tf remote executor consistency naming
  state_bucket_name = var.state_bucket_name != "" ? var.state_bucket_name : "${var.project_name}-state-${local.account_id}"
  state_bucket_arn  = "arn:aws:s3:::${local.state_bucket_name}"
  engine_name       = var.engine_name != "" ? var.engine_name : var.project_name
  # config0-addon installs lock via the S3 native lock file (tofu >= 1.10);
  # no DynamoDB lock table exists in the tenant account.
  use_lock_table = var.install_mode == "standalone"
}

# Foundation KMS key (alias/<project>-foundation)
data "aws_kms_alias" "foundation" {
  name = "alias/${var.project_name}-foundation"
}

# Foundation buckets on deterministic names
data "aws_s3_bucket" "tmp" {
  bucket = "${var.project_name}-tmp-${local.account_id}"
}

data "aws_s3_bucket" "package" {
  bucket = "${var.project_name}-package-${local.account_id}"
}

data "aws_s3_bucket" "done" {
  bucket = "${var.project_name}-done-${local.account_id}"
}

# Engine init_job Lambda (deployed by the engine repo with prefix <engine_name>)
data "aws_lambda_function" "engine_init" {
  function_name = "${local.engine_name}-init-job"
}

# Bootstrap lock table (standalone installs only; config0-addon has none)
data "aws_dynamodb_table" "locks" {
  count = local.use_lock_table ? 1 : 0
  name  = "${var.project_name}-tf-locks"
}
