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
}

run "valid_exact_hub_role_arn" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.executor_readonly.assume_role_policy).Statement[0].Principal.AWS == "arn:aws:iam::111111111111:root"
    error_message = "trust principal must be canonical hub account root"
  }

  assert {
    condition = jsondecode(aws_iam_role.executor_readonly.assume_role_policy).Statement[0].Condition.StringEquals["aws:PrincipalArn"] == [
      "arn:aws:iam::111111111111:role/openci-tf-hub-lambda-exec",
      "arn:aws:iam::111111111111:role/openci-tf-run-folder-prepare-and-submit",
    ]
    error_message = "allowed PrincipalArn values must be canonical and derived from the validated hub account id"
  }
}

run "reject_wrong_role_name" {
  command = plan

  variables {
    hub_lambda_exec_role_arn = "arn:aws:iam::111111111111:role/openci-tf-other-role"
  }

  expect_failures = [aws_iam_role.executor_readonly]
}

run "reject_role_path" {
  command = plan

  variables {
    hub_lambda_exec_role_arn = "arn:aws:iam::111111111111:role/path/openci-tf-hub-lambda-exec"
  }

  expect_failures = [aws_iam_role.executor_readonly]
}

run "reject_short_account_id" {
  command = plan

  variables {
    hub_lambda_exec_role_arn = "arn:aws:iam::11111111111:role/openci-tf-hub-lambda-exec"
  }

  expect_failures = [aws_iam_role.executor_readonly]
}

run "reject_malformed_string" {
  command = plan

  variables {
    hub_lambda_exec_role_arn = "not-an-arn"
  }

  expect_failures = [aws_iam_role.executor_readonly]
}

run "reject_extra_colon_segments" {
  command = plan

  variables {
    hub_lambda_exec_role_arn = "arn:aws:iam::111111111111:role/openci-tf-hub-lambda-exec:extra"
  }

  expect_failures = [aws_iam_role.executor_readonly]
}
