# Cross-stack wiring follows the deploy root's deterministic-name convention.
data "aws_apigatewayv2_apis" "core" {
  name          = "${var.project_name}-webhook"
  protocol_type = "HTTP"
}

data "aws_apigatewayv2_api" "core" {
  api_id = one(data.aws_apigatewayv2_apis.core.ids)
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}
