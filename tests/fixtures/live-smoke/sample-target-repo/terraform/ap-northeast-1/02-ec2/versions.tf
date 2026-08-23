terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Reuses the legacy combined-state key so existing workload addresses stay in place.
  backend "s3" {
    bucket         = "openci-tf-state-REPLACE_MAIN_ACCOUNT"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/ap-northeast-1.tfstate"
    region         = "us-east-1"
    dynamodb_table = "openci-tf-tf-locks"
    encrypt        = true
  }
}
