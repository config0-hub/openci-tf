provider "aws" {
  region = var.aws_region
}

locals {
  function_name                = "${var.project_name}-console"
  lambda_zip                   = abspath("${path.module}/../../frontend/build/openci-tf-console.zip")
  core_stage_arn               = "${data.aws_apigatewayv2_api.core.execution_arn}/$default"
  console_token_parameter_name = "/openci-tf/install/${var.project_name}/console_token"
  console_token_parameter_arn  = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.console_token_parameter_name}"
}

resource "aws_cloudwatch_log_group" "console" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_iam_role" "console" {
  name = "${var.project_name}-console"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "console" {
  name = "${var.project_name}-console"
  role = aws_iam_role.console.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.console.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = local.console_token_parameter_arn
      },
      {
        Effect = "Allow"
        Action = ["execute-api:Invoke"]
        Resource = [
          "${local.core_stage_arn}/GET/runs",
          "${local.core_stage_arn}/GET/runs/*",
          "${local.core_stage_arn}/POST/runs",
          "${local.core_stage_arn}/GET/repos",
          "${local.core_stage_arn}/GET/accounts",
          "${local.core_stage_arn}/GET/locks",
          "${local.core_stage_arn}/GET/gates",
        ]
      },
    ]
  })
}

resource "aws_lambda_function" "console" {
  function_name = local.function_name
  description   = "openci-tf operator console and SigV4 core API proxy"
  role          = aws_iam_role.console.arn
  runtime       = "nodejs20.x"
  handler       = "server-dist/lambda.handler"
  filename      = local.lambda_zip
  # Keep validate usable before a local build; the deploy recipe always builds
  # the zip first, so real plans and applies receive the content hash.
  source_code_hash = fileexists(local.lambda_zip) ? filebase64sha256(local.lambda_zip) : null
  memory_size      = 256
  timeout          = 30

  environment {
    variables = {
      CONSOLE_STATIC_ROOT     = "/var/task/dist"
      CONSOLE_TOKEN_PARAMETER = local.console_token_parameter_name
      OPENCI_TF_API_BASE         = data.aws_apigatewayv2_api.core.api_endpoint
    }
  }

  depends_on = [aws_iam_role_policy.console]

  tags = merge(var.tags, {
    Name = local.function_name
  })
}

resource "aws_lambda_function_url" "console" {
  function_name      = aws_lambda_function.console.function_name
  authorization_type = "NONE"
}

# Function URL auth is intentionally NONE so a browser can load the SPA shell.
# The Hono application enforces the shared bearer token on every /api request.
resource "aws_lambda_permission" "function_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.console.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# New Function URLs require both URL invocation and function invocation
# permissions; this second statement is restricted to the Function URL path.
resource "aws_lambda_permission" "function_url_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.console.function_name
  principal                = "*"
  invoked_via_function_url = true
}
