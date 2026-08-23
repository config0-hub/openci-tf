# Cross-stack wiring via data-source lookups on deterministic names.
# Foundation, engine, and bootstrap resources are discovered — not hand-threaded.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # ref 4353245 - openci-tf remote executor consistency naming
  state_bucket_name = "${var.project_name}-state-${local.account_id}"
  state_bucket_arn  = "arn:aws:s3:::${local.state_bucket_name}"
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

# Engine init_job Lambda (deployed by the engine repo with project prefix <project>)
data "aws_lambda_function" "engine_init" {
  function_name = "${var.project_name}-init-job"
}

# Bootstrap lock table
data "aws_dynamodb_table" "locks" {
  name = "${var.project_name}-tf-locks"
}
