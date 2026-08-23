output "function_url" {
  description = "Public console shell URL; API requests require the application bearer token"
  value       = aws_lambda_function_url.console.function_url
}

output "execution_role_arn" {
  description = "Role ARN to add to the deploy stack's api_caller_policy_json"
  value       = aws_iam_role.console.arn
}

output "console_token_parameter_name" {
  description = "SSM SecureString parameter fetched by the console at cold start"
  value       = local.console_token_parameter_name
}
