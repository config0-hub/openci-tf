mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111111111111"
    }
  }
}

variables {
  role_prefix        = "openci-tf"
  target_account_ids = []
  state_bucket_arn   = "arn:aws:s3:::openci-tf-state-111111111111"
  lock_table_arn     = "arn:aws:dynamodb:us-east-1:111111111111:table/openci-tf-tf-locks"
}

override_resource {
  target = aws_iam_role.hub_lambda_exec
  values = {
    arn  = "arn:aws:iam::111111111111:role/openci-tf-hub-lambda-exec"
    id   = "openci-tf-hub-lambda-exec"
    name = "openci-tf-hub-lambda-exec"
  }
}

override_resource {
  target = aws_iam_role.executor_local[0]
  values = {
    arn  = "arn:aws:iam::111111111111:role/openci-tf-executor-local"
    id   = "openci-tf-executor-local"
    name = "openci-tf-executor-local"
  }
}

run "fresh_plan_preserves_executor_local_address" {
  command = plan

  assert {
    condition     = aws_iam_role.executor_local[0].name == "openci-tf-executor-local"
    error_message = "legacy executor_local must remain at its original address and name"
  }

  assert {
    condition     = aws_iam_role.executor_readonly.name == "openci-tf-executor-readonly"
    error_message = "upgrade must plan the new executor_readonly role alongside legacy executor_local"
  }
}

run "retired_legacy_skips_executor_local" {
  command = plan

  variables {
    provision_legacy_executor_local = false
  }

  assert {
    condition     = length(aws_iam_role.executor_local) == 0
    error_message = "provision_legacy_executor_local=false must not plan legacy executor_local"
  }
}
