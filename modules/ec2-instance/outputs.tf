output "instance_id" {
  description = "ID of the EC2 instance"
  value       = aws_instance.workspace.id
}

output "instance_public_ip" {
  description = "Public IP of the EC2 instance"
  value       = aws_instance.workspace.public_ip
}

output "instance_private_ip" {
  description = "Private IP of the EC2 instance"
  value       = aws_instance.workspace.private_ip
}

output "security_group_id" {
  description = "ID of the created security group (if created)"
  value       = length(var.security_group_ids) == 0 ? aws_security_group.workspace[0].id : null
}

output "vpc_id" {
  description = "ID of the VPC"
  value       = local.selected_vpc_id
}

output "subnet_id" {
  description = "ID of the subnet used"
  value       = local.selected_subnet_id
}

output "ami_id" {
  description = "ID of the AMI used for the instance"
  value       = local.selected_ami_id
}

output "ami_boot_mode" {
  description = "Boot mode of the resolved Ubuntu AMI, or null when ami_id is supplied"
  value       = local.selected_ami_boot_mode
}

output "root_volume_id" {
  description = "ID of the root EBS volume attached to the instance"
  value       = aws_instance.workspace.root_block_device[0].volume_id
}

output "ssm_start_session_command" {
  description = "CLI command to start an SSM Session Manager shell"
  value       = local.session_manager_enabled ? "aws ssm start-session --target ${aws_instance.workspace.id}" : null
}

output "ssm_parameter_tailscale_arn" {
  description = "ARN of the Tailscale auth key SSM parameter"
  value       = try(aws_ssm_parameter.tailscale_auth_key[0].arn, null)
  sensitive   = true
}

output "ssm_parameter_instance_name_arn" {
  description = "ARN of the instance name SSM parameter"
  value       = aws_ssm_parameter.instance_name.arn
}

output "iam_instance_profile_name" {
  description = "Name of the IAM instance profile for CloudWatch Logs, SSM, and parameter access"
  value       = length(aws_iam_instance_profile.ssm) > 0 ? aws_iam_instance_profile.ssm[0].name : null
}

output "cloudwatch_log_group_name" {
  description = "CloudWatch Log Group that receives EC2 bootstrap, cloud-init, and instance logs"
  value       = aws_cloudwatch_log_group.workspace.name
}
