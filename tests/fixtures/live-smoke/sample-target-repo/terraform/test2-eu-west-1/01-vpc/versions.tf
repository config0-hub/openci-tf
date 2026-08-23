terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket         = "openci-tf-state-REPLACE_SECONDARY_ACCOUNT"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/test2-eu-west-1/01-vpc.tfstate"
    region         = "us-east-1"
    dynamodb_table = "openci-tf-tf-locks"
    encrypt        = true
  }
}
