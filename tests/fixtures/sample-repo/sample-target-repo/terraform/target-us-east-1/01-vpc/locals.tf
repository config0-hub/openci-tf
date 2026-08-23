locals {
  aws_region      = "us-east-1"
  vpc_cidr        = "10.43.0.0/16"
  allowed_account = "222222222222"
  name_prefix     = "openci-tf-tracer-target-use1"

  # User request "US-East-11" is interpreted as us-east-1 (no valid AWS region us-east-11).
  lifecycle_start_date = "2026-01-01"

  common_tags = {
    ManagedBy   = "terraform"
    Project     = "example-openci-tf-target"
    Environment = "disposable-test"
    Region      = local.aws_region
    Disposable  = "true"
    ExpiresOn   = "2026-01-08"
    Owner       = "example-team"
    Purpose     = "openci-tf-cross-account-tracer"
    AccountRole = "remote-target"
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

resource "terraform_data" "account_guard" {
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == local.allowed_account
      error_message = "Refusing to run in account ${data.aws_caller_identity.current.account_id}; expected ${local.allowed_account}."
    }
  }
}

provider "aws" {
  region              = local.aws_region
  allowed_account_ids = [local.allowed_account]

  default_tags {
    tags = local.common_tags
  }
}
