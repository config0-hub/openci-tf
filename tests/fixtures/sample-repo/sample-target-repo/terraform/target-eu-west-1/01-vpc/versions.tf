terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket         = "openci-tf-state-222222222222"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/target-eu-west-1/01-vpc.tfstate"
    region         = "us-east-1"
    encrypt        = true
  }
}
