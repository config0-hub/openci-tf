# DynamoDB
output "settings_table_name" {
  value = aws_dynamodb_table.settings.name
}

output "settings_table_arn" {
  value = aws_dynamodb_table.settings.arn
}

# Lambda
output "lambda_arns" {
  description = "Map of Lambda function name to ARN"
  value = {
    for k, v in aws_lambda_function.functions : k => v.arn
  }
}

output "lambda_invoke_arns" {
  description = "Map of Lambda function name to invoke ARN"
  value = {
    for k, v in aws_lambda_function.functions : k => v.invoke_arn
  }
}

# Step Function
output "state_machine_arn" {
  value = aws_sfn_state_machine.openci_tf.arn
}

output "apply_state_machine_arn" {
  value = aws_sfn_state_machine.openci_tf_apply.arn
}

output "destroy_state_machine_arn" {
  value = aws_sfn_state_machine.openci_tf_destroy.arn
}

# API Gateway
output "webhook_url" {
  description = "Base URL for webhooks: POST {url}/{trigger_id}"
  value       = "${aws_apigatewayv2_stage.default.invoke_url}/webhook"
}

output "api_id" {
  value = aws_apigatewayv2_api.webhook.id
}

output "run_registry_table_name" {
  value = aws_dynamodb_table.run_registry.name
}

output "api_url" {
  description = "Base URL for AWS IAM core API routes"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "alarm_topic_arn" { value = aws_sns_topic.alarms.arn }
