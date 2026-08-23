data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
  foundation_kms_context = {
    StringLike = {
      "kms:EncryptionContext:aws:s3:arn" = [
        "${var.tmp_bucket_arn}/*",
        "${var.done_bucket_arn}/*",
      ]
    }
  }
  foundation_kms_via_s3 = {
    StringEquals = {
      "kms:ViaService" = "s3.${local.region}.amazonaws.com"
    }
  }
  report_all_pointer_kms_context = {
    StringLike = {
      "kms:EncryptionContext:aws:s3:arn" = "${var.tmp_bucket_arn}/openci-tf/*/pr-*/report-all.env"
    }
  }

  # Constructed ARNs to break the circular dependency between Lambdas and Step Function.
  # Lambda env vars/IAM need the Step Function ARN, and the Step Function definition/IAM
  # needs Lambda ARNs. By constructing both from predictable naming, neither terraform
  # resource depends on the other's attributes.
  step_function_arn         = "arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.project_name}"
  apply_step_function_arn   = "arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.project_name}-apply"
  destroy_step_function_arn = "arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.project_name}-destroy"

  lambda_arns = {
    for name, _ in local.lambdas :
    name => "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.project_name}-${name}"
  }
}
