# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
locals {
  names       = ["prepare-and-submit", "poll-done", "collect", "persist-retry-attempt", "write-failure-manifest"]
  lane_suffix = var.lane == "read" ? "" : "-${var.lane}"
  lane_label  = var.lane == "read" ? "" : "-${var.lane}"
  allowed_actions = (
    var.lane == "read" ? ["plan", "plan_destroy", "drift", "report"] :
    var.lane == "apply" ? ["apply"] :
    ["destroy"]
  )
  mutation_lane                 = var.lane != "read"
  state_machine_name            = "${var.project_name}-run-folder${local.lane_suffix}"
  resource_name_label           = "${var.project_name}-run-folder${local.lane_label}"
  engine_codebuild_project_name = local.mutation_lane ? var.engine_codebuild_project_name : ""
  prepare_engine_submit_statements = concat(
    local.mutation_lane ? [
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = var.engine_codebuild_state_machine_arn },
      { Effect = "Allow", Action = ["states:DescribeExecution"], Resource = "${replace(var.engine_codebuild_state_machine_arn, ":stateMachine:", ":execution:")}:*" },
      { Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:ListBuildsForProject"], Resource = local.engine_codebuild_project_arn },
    ] : [],
    local.mutation_lane ? [] : [
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = var.engine_init_lambda_arn },
    ],
  )
}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
resource "aws_iam_role" "lambda" {
  for_each           = toset(local.names)
  name               = "${local.resource_name_label}-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role" "sfn" {
  name               = "${local.resource_name_label}-sfn"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "states.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
