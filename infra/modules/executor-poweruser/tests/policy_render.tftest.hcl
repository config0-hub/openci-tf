mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "222222222222"
    }
  }
  mock_data "aws_region" {
    defaults = {
      name = "us-east-1"
    }
  }
}

variables {
  role_prefix              = "openci-tf"
  hub_lambda_exec_role_arn = "arn:aws:iam::111111111111:role/openci-tf-hub-lambda-exec"
  state_bucket_arn         = "arn:aws:s3:::openci-tf-state-222222222222"
}

run "render_policy" {
  command = plan

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.executor_poweruser.policy).Statement) >= 5
    error_message = "executor-poweruser inline policy must render all guard statements"
  }

  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_role_policy.executor_poweruser.policy).Statement :
      try(statement.Sid, "") == "DenyProtectedHubResources"
    ])
    error_message = "executor-poweruser policy must deny mutation of protected hub resources"
  }
}
