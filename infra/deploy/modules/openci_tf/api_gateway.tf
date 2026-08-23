resource "aws_apigatewayv2_api" "webhook" {
  name          = "${var.project_name}-webhook"
  protocol_type = "HTTP"

  tags = merge(var.tags, {
    Name = "${var.project_name}-webhook"
  })
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_integration" "trigger" {
  api_id                 = aws_apigatewayv2_api.webhook.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.functions["trigger-stepf"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.webhook.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.functions["api"].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webhook" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhook/{trigger_id}"
  target    = "integrations/${aws_apigatewayv2_integration.trigger.id}"
}

resource "aws_apigatewayv2_route" "create_run" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "POST /runs"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "list_runs" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /runs"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "list_repos" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /repos"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "list_accounts" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /accounts"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "list_locks" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /locks"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "get_gates" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /gates"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "get_run" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /runs/{run_id}"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "list_folders" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /runs/{run_id}/folders"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "get_manifest" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /runs/{run_id}/folders/{folder}/manifest"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "get_artifact" {
  api_id             = aws_apigatewayv2_api.webhook.id
  route_key          = "GET /runs/{run_id}/folders/{folder}/artifacts"
  authorization_type = "AWS_IAM"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["trigger-stepf"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}

resource "aws_lambda_permission" "apigw_api" {
  statement_id  = "AllowAPIGatewayInvokeApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.functions["api"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}
