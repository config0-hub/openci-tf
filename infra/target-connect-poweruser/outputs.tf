output "executor_poweruser_role_arn" {
  description = "ARN of the executor-poweruser role created in the target account"
  value       = module.executor_poweruser.executor_poweruser_role_arn
}
