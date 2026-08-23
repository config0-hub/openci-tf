mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "222222222222"
    }
  }
}

variables {
  role_prefix              = "openci-tf"
  hub_lambda_exec_role_arn = "arn:aws:iam::111111111111:role/openci-tf-hub-lambda-exec"
  state_bucket_arn         = "arn:aws:s3:::openci-tf-state-222222222222"
  lock_table_arn           = "arn:aws:dynamodb:us-east-1:222222222222:table/openci-tf-tf-locks"
}

override_resource {
  target = aws_iam_policy.executor_readonly_permissions_boundary
  values = {
    arn = "arn:aws:iam::222222222222:policy/openci-tf-executor-readonly-permissions-boundary"
  }
  override_during = plan
}

override_resource {
  target = aws_iam_role.executor_readonly
  values = {
    permissions_boundary = "arn:aws:iam::222222222222:policy/openci-tf-executor-readonly-permissions-boundary"
  }
  override_during = plan
}

run "render_policy" {
  command = plan

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.executor_readonly.policy).Statement) >= 8
    error_message = "executor-readonly inline policy must render all guard statements"
  }

  assert {
    condition     = aws_iam_role.executor_readonly.permissions_boundary == aws_iam_policy.executor_readonly_permissions_boundary.arn
    error_message = "executor-readonly role must attach the managed permissions boundary"
  }

  assert {
    condition     = aws_iam_policy.executor_readonly_permissions_boundary.name == "openci-tf-executor-readonly-permissions-boundary"
    error_message = "permissions boundary policy name must be deterministic"
  }
}
