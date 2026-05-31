output "instance_id" {
  description = "ID of the EC2 instance created by the module"
  value       = module.ec2_instance.instance_id
}

output "ssm_start_session_command" {
  description = "AWS CLI command for opening a Session Manager shell"
  value       = module.ec2_instance.ssm_start_session_command
}

output "cloudwatch_log_group_name" {
  description = "CloudWatch Log Group that receives EC2 bootstrap, cloud-init, and instance logs"
  value       = module.ec2_instance.cloudwatch_log_group_name
}
