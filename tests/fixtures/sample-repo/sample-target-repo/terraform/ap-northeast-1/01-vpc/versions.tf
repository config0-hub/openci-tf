terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Checked-in backend for plain `tofu init` (openci-tf does not inject -backend-config).
  backend "s3" {
    bucket         = "openci-tf-state-111111111111"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/ap-northeast-1/01-vpc.tfstate"
    region         = "us-east-1"
    dynamodb_table = "openci-tf-tf-locks"
    encrypt        = true
  }
}
