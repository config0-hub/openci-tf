locals {
  aws_region      = "us-east-1"
  vpc_cidr        = "10.43.0.0/16"
  allowed_account = "REPLACE_SECONDARY_ACCOUNT"
  name_prefix     = "openci-tf-tracer-test2-use1"

  # User request "US-East-11" is interpreted as us-east-1 (no valid AWS region us-east-11).
  approved_test_date = "2026-08-09"

  common_tags = {
    ManagedBy   = "terraform"
    Project     = "example-openci-tf-test2"
    Environment = "disposable-test"
    Region      = local.aws_region
    Disposable  = "true"
    ExpiresOn   = "2026-08-16"
    Owner       = "example-team"
    Purpose     = "openci-tf-cross-account-tracer"
    AccountRole = "test2-target"
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
