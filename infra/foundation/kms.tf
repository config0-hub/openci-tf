data "aws_iam_policy_document" "foundation_key" {
  statement {
    sid       = "AccountRootOnly"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}
resource "aws_kms_key" "foundation" {
  description         = "openci-tf foundation key"
  policy              = data.aws_iam_policy_document.foundation_key.json
  enable_key_rotation = true
}
resource "aws_kms_alias" "foundation" {
  name          = "alias/${var.name_prefix}-foundation"
  target_key_id = aws_kms_key.foundation.key_id
}
