data "terraform_remote_state" "vpc" {
  backend = "s3"

  config = {
    bucket         = "openci-tf-state-REPLACE_MAIN_ACCOUNT"
    key            = "targets/<REPO_ORG>/<REPO_NAME>/terraform/eu-west-1/01-vpc.tfstate"
    region         = "us-east-1"
    dynamodb_table = "openci-tf-tf-locks"
    encrypt        = true
  }
}
