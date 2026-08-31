terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Checked-in backend for plain `tofu init` (bucket/key/region only, any
  # version). openci-tf's platform runs pass -backend-config=use_lockfile=true
  # at init; tofu/terraform >= 1.10 is recommended so manual runs lock too
  # (older versions run unlocked).
  backend "s3" {
    bucket         = "openci-tf-state-111111111111"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/ap-northeast-1/01-vpc.tfstate"
    region         = "us-east-1"
    encrypt        = true
  }
}
