data "terraform_remote_state" "vpc" {
  backend = "s3"

  config = {
    bucket         = "openci-tf-state-111111111111"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/ap-northeast-1/01-vpc.tfstate"
    region         = "us-east-1"
    dynamodb_table = "openci-tf-tf-locks"
    encrypt        = true
  }
}
