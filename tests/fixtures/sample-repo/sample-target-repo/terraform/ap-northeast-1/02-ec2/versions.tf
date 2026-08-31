terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Reuses the shared region-state key so existing workload addresses stay in place.
  backend "s3" {
    bucket         = "openci-tf-state-111111111111"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/ap-northeast-1.tfstate"
    region         = "us-east-1"
    encrypt        = true
  }
}
