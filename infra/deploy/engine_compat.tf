# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# Compatibility policy for the unmodified execution engine. The engine is
# deployed by config0 onboarding BEFORE this add-on exists, so it cannot list
# openci-tf's foundation buckets in its own additional_package_bucket_arns /
# additional_result_bucket_arns inputs. This file applies that same grant from
# the add-on side: it attaches inline policies to the already-deployed engine
# roles so a destroy of this install revokes them and leaves the engine at its
# defaults.
#
# Foundation buckets are SSE-KMS with the openci-tf foundation key, so a role
# that reads the package bucket needs kms:Decrypt and a role that writes the
# done bucket needs kms:GenerateDataKey, in addition to the S3 action.
#
# Grant table, mirroring the engine's own iam.tf (aws-execution-engine
# infra/02-deploy/iam.tf): the package bucket is an additional_package bucket
# (GetObject to init-job, worker, codebuild) and the done bucket is an
# additional_result bucket (PutObject to worker, codebuild, finalizer):
#   init-job   read lane      GetObject(package)              + kms:Decrypt
#   worker     read lane      GetObject(package) PutObject(done) + kms (below)
#   codebuild  mutation lane  GetObject(package) PutObject(done) + kms (below)
#   finalizer  both lanes     PutObject(done)                 + kms:GenerateDataKey
# The codebuild-workflow (state-machine execution) role touches no S3, so it is
# not granted here.
data "aws_iam_role" "engine_init" {
  name = "${local.engine_name}-init-job"
}

data "aws_iam_role" "engine_worker" {
  name = "${local.engine_name}-worker"
}

data "aws_iam_role" "engine_codebuild" {
  name = "${local.engine_name}-codebuild"
}

data "aws_iam_role" "engine_finalizer" {
  name = "${local.engine_name}-finalizer"
}

# init-job validates every read-lane submission with head_object on the package
# object (aws_exe_sys/init_job/validate.py); HEAD on an SSE-KMS object needs
# kms:Decrypt as well as s3:GetObject, and the foundation key policy is
# account-root-only, so the grant must be identity-based here.
resource "aws_iam_role_policy" "engine_init_foundation" {
  name = "${var.project_name}-init-foundation"
  role = data.aws_iam_role.engine_init.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${data.aws_s3_bucket.package.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = data.aws_kms_alias.foundation.target_key_arn
      },
    ]
  })
}

# Read-lane worker Lambda: reads the package zip, writes the done marker. Its
# foundation KMS grant is engine_worker_foundation_kms below.
resource "aws_iam_role_policy" "engine_worker_foundation_s3" {
  name = "${var.project_name}-worker-foundation-s3"
  role = data.aws_iam_role.engine_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${data.aws_s3_bucket.package.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${data.aws_s3_bucket.done.arn}/*"
      },
    ]
  })
}

# Mutation-lane CodeBuild service role: reads the package zip, writes the done
# marker. Its foundation KMS grant is engine_codebuild_foundation_kms below.
resource "aws_iam_role_policy" "engine_codebuild_foundation_s3" {
  name = "${var.project_name}-codebuild-foundation-s3"
  role = data.aws_iam_role.engine_codebuild.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${data.aws_s3_bucket.package.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${data.aws_s3_bucket.done.arn}/*"
      },
    ]
  })
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
