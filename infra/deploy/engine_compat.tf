# Compatibility policy for the unmodified execution engine: foundation buckets use
# the openci-tf KMS key, so engine roles that touch foundation SSE-KMS objects need
# narrowly scoped foundation-key permissions in addition to their engine defaults.
data "aws_iam_role" "engine_worker" {
  name = "${var.project_name}-worker"
}

data "aws_iam_role" "engine_codebuild" {
  name = "${var.project_name}-codebuild"
}

data "aws_iam_role" "engine_finalizer" {
  name = "${var.project_name}-finalizer"
}

resource "aws_iam_role_policy" "engine_worker_foundation_kms" {
  name = "${var.project_name}-worker-foundation-kms"
  role = data.aws_iam_role.engine_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
      Resource = data.aws_kms_alias.foundation.target_key_arn
    }]
  })
}

resource "aws_iam_role_policy" "engine_codebuild_foundation_kms" {
  name = "${var.project_name}-codebuild-foundation-kms"
  role = data.aws_iam_role.engine_codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
      Resource = data.aws_kms_alias.foundation.target_key_arn
    }]
  })
}

resource "aws_iam_role_policy" "engine_finalizer_foundation_done" {
  name = "${var.project_name}-finalizer-foundation-done"
  role = data.aws_iam_role.engine_finalizer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${data.aws_s3_bucket.done.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey"]
        Resource = data.aws_kms_alias.foundation.target_key_arn
      },
    ]
  })
}
