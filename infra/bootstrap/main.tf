terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

locals {
  # ref 4353245 - openci-tf remote executor consistency naming
  # Deterministic default: openci-tf-state-<account-id>; override via var.state_bucket_name.
  state_bucket_name = var.state_bucket_name != "" ? var.state_bucket_name : "${var.project_name}-state-${data.aws_caller_identity.current.account_id}"
  lock_table_name   = "${var.project_name}-tf-locks"
}

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket_name

  tags = {
    Name    = local.state_bucket_name
    Project = var.project_name
    # Positive ownership proof read by the engine installer's adoption logic.
    ManagedBy = "openci-tf-bootstrap"
    Purpose   = "terraform-state"
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "locks" {
  name         = local.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = {
    Name    = local.lock_table_name
    Project = var.project_name
    # Positive ownership proof required by bootstrap-destroy before deletion.
    ManagedBy = "openci-tf-bootstrap"
    Purpose   = "terraform-state-locking"
  }
}
