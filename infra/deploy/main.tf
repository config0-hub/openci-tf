# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
provider "aws" {
  region = var.aws_region
}

# ref 4353245 - openci-tf remote executor consistency naming
locals {
  executor_readonly_role_name  = "${var.project_name}-executor-readonly"
  executor_poweruser_role_name = "${var.project_name}-executor-poweruser"
  executor_remote_role_name    = "${var.project_name}-executor-remote"
  executor_local_role_name     = "${var.project_name}-executor-local"
  ecr_image_uri                = "${module.ecr.repository_url}@${data.aws_ecr_image.openci_tf.image_digest}"
}

module "ecr" {
  source       = "./modules/ecr"
  project_name = var.project_name
  tags         = var.tags
}

data "aws_ecr_image" "openci_tf" {
  repository_name = var.project_name
  image_tag       = var.image_tag
  depends_on      = [module.ecr]
}

module "hub_setup" {
  source             = "../modules/hub-setup"
  role_prefix        = var.project_name
  target_account_ids = var.target_account_ids
  state_bucket_arn   = local.state_bucket_arn
  lock_table_arn     = data.aws_dynamodb_table.locks.arn
  enable_apply       = var.enable_apply
}

data "aws_sfn_state_machine" "engine_codebuild" {
  name = "${var.project_name}-codebuild"
}

module "run_folder" {
  source                     = "./modules/run_folder"
  project_name               = var.project_name
  lane                       = "read"
  ecr_image_uri              = local.ecr_image_uri
  kms_key_arn                = data.aws_kms_alias.foundation.target_key_arn
  package_bucket_name        = data.aws_s3_bucket.package.bucket
  package_bucket_arn         = data.aws_s3_bucket.package.arn
  tmp_bucket_name            = data.aws_s3_bucket.tmp.bucket
  tmp_bucket_arn             = data.aws_s3_bucket.tmp.arn
  done_bucket_name           = data.aws_s3_bucket.done.bucket
  done_bucket_arn            = data.aws_s3_bucket.done.arn
  engine_init_lambda_arn     = data.aws_lambda_function.engine_init.arn
  engine_init_lambda_name    = data.aws_lambda_function.engine_init.function_name
  run_history_retention_days = var.run_history_retention_days
  tmp_lifecycle_days         = var.tmp_lifecycle_days
  package_lifecycle_days     = var.package_lifecycle_days
  done_lifecycle_days        = var.done_lifecycle_days
  plan_retention_days        = var.plan_retention_days
  aws_console_start_url      = var.aws_console_start_url
  aws_console_role_name      = var.aws_console_role_name
  tags                       = var.tags
}

module "run_folder_apply" {
  source                             = "./modules/run_folder"
  project_name                       = var.project_name
  lane                               = "apply"
  ecr_image_uri                      = local.ecr_image_uri
  kms_key_arn                        = data.aws_kms_alias.foundation.target_key_arn
  package_bucket_name                = data.aws_s3_bucket.package.bucket
  package_bucket_arn                 = data.aws_s3_bucket.package.arn
  tmp_bucket_name                    = data.aws_s3_bucket.tmp.bucket
  tmp_bucket_arn                     = data.aws_s3_bucket.tmp.arn
  done_bucket_name                   = data.aws_s3_bucket.done.bucket
  done_bucket_arn                    = data.aws_s3_bucket.done.arn
  engine_init_lambda_arn             = data.aws_lambda_function.engine_init.arn
  engine_init_lambda_name            = data.aws_lambda_function.engine_init.function_name
  engine_codebuild_state_machine_arn = data.aws_sfn_state_machine.engine_codebuild.arn
  engine_codebuild_project_name      = "${var.project_name}-worker"
  run_history_retention_days         = var.run_history_retention_days
  tmp_lifecycle_days                 = var.tmp_lifecycle_days
  package_lifecycle_days             = var.package_lifecycle_days
  done_lifecycle_days                = var.done_lifecycle_days
  plan_retention_days                = var.plan_retention_days
  aws_console_start_url              = var.aws_console_start_url
  aws_console_role_name              = var.aws_console_role_name
  tags                               = var.tags
}

module "run_folder_destroy" {
  source                             = "./modules/run_folder"
  project_name                       = var.project_name
  lane                               = "destroy"
  ecr_image_uri                      = local.ecr_image_uri
  kms_key_arn                        = data.aws_kms_alias.foundation.target_key_arn
  package_bucket_name                = data.aws_s3_bucket.package.bucket
  package_bucket_arn                 = data.aws_s3_bucket.package.arn
  tmp_bucket_name                    = data.aws_s3_bucket.tmp.bucket
  tmp_bucket_arn                     = data.aws_s3_bucket.tmp.arn
  done_bucket_name                   = data.aws_s3_bucket.done.bucket
  done_bucket_arn                    = data.aws_s3_bucket.done.arn
  engine_init_lambda_arn             = data.aws_lambda_function.engine_init.arn
  engine_init_lambda_name            = data.aws_lambda_function.engine_init.function_name
  engine_codebuild_state_machine_arn = data.aws_sfn_state_machine.engine_codebuild.arn
  engine_codebuild_project_name      = "${var.project_name}-worker"
  run_history_retention_days         = var.run_history_retention_days
  tmp_lifecycle_days                 = var.tmp_lifecycle_days
  package_lifecycle_days             = var.package_lifecycle_days
  done_lifecycle_days                = var.done_lifecycle_days
  plan_retention_days                = var.plan_retention_days
  aws_console_start_url              = var.aws_console_start_url
  aws_console_role_name              = var.aws_console_role_name
  tags                               = var.tags
}

module "openci_tf" {
  source        = "./modules/openci_tf"
  project_name  = var.project_name
  ecr_image_uri = local.ecr_image_uri
  tags          = var.tags

  engine_init_lambda_name              = data.aws_lambda_function.engine_init.function_name
  engine_init_lambda_arn               = data.aws_lambda_function.engine_init.arn
  tmp_bucket_name                      = data.aws_s3_bucket.tmp.bucket
  tmp_bucket_arn                       = data.aws_s3_bucket.tmp.arn
  done_bucket_name                     = data.aws_s3_bucket.done.bucket
  done_bucket_arn                      = data.aws_s3_bucket.done.arn
  package_bucket_name                  = data.aws_s3_bucket.package.bucket
  kms_key_arn                          = data.aws_kms_alias.foundation.target_key_arn
  run_history_retention_days           = var.run_history_retention_days
  run_folder_max_concurrency           = var.run_folder_max_concurrency
  tmp_lifecycle_days                   = var.tmp_lifecycle_days
  package_lifecycle_days               = var.package_lifecycle_days
  done_lifecycle_days                  = var.done_lifecycle_days
  plan_retention_days                  = var.plan_retention_days
  run_folder_state_machine_arn         = module.run_folder.state_machine_arn
  run_folder_apply_state_machine_arn   = module.run_folder_apply.state_machine_arn
  run_folder_destroy_state_machine_arn = module.run_folder_destroy.state_machine_arn
  assume_role_arns                     = compact([module.hub_setup.executor_local_role_arn, module.hub_setup.executor_readonly_role_arn])
  api_caller_policy_json               = var.api_caller_policy_json
  aws_console_start_url                = var.aws_console_start_url
  aws_console_role_name                = var.aws_console_role_name
}
