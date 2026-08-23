locals {
  aws_region      = "ap-northeast-1"
  vpc_cidr        = "10.41.0.0/16"
  allowed_account = "REPLACE_MAIN_ACCOUNT"
  name_prefix     = "openci-tf-tracer-apne1"

  # Bump approved_test_date when re-approving a disposable test window; ExpiresOn is +7 days.
  approved_test_date = "2026-08-06"

  common_tags = {
    ManagedBy   = "terraform"
    Project     = "example-openci-tf-multiregion"
    Environment = "disposable-test"
    Region      = local.aws_region
    Disposable  = "true"
    ExpiresOn   = "2026-08-13"
    Owner       = "example-team"
    Purpose     = "openci-tf-multiregion-tracer"
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
