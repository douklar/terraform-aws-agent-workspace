output "instance_id" {
  description = "ID of the EC2 instance created by the module"
  value       = module.harbor_workspace.instance_id
}

output "ssm_start_session_command" {
  description = "AWS CLI command for opening a Session Manager shell"
  value       = module.harbor_workspace.ssm_start_session_command
}

output "scheduler_lambda_name" {
  description = "Name of the scheduler Lambda function"
  value       = module.harbor_workspace.scheduler_lambda_name
}
