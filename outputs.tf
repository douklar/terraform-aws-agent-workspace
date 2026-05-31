output "instance_id" {
  description = "ID of the managed instance."
  value       = module.ec2_instance.instance_id
}

output "instance_public_ip" {
  description = "Public IP of the managed instance (if assigned)."
  value       = module.ec2_instance.instance_public_ip
}

output "ami_boot_mode" {
  description = "Boot mode of the AMI used for the managed instance."
  value       = module.ec2_instance.ami_boot_mode
}

output "ami_id" {
  description = "AMI ID used for the managed instance."
  value       = module.ec2_instance.ami_id
}

output "root_volume_id" {
  description = "Root EBS volume ID for the managed instance."
  value       = module.ec2_instance.root_volume_id
}

output "scheduler_lambda_name" {
  description = "Lambda function name handling schedules and AMI lifecycle."
  value       = aws_lambda_function.scheduler.function_name
}

output "scheduler_lambda_arn" {
  description = "Lambda function ARN handling schedules and AMI lifecycle."
  value       = aws_lambda_function.scheduler.arn
}

output "ami_transfer_lambda_name" {
  description = "Dedicated Lambda function name for manual AMI copy/export operations, or null when transfer is disabled."
  value       = local.ami_transfer_enabled ? aws_lambda_function.ami_transfer[0].function_name : null
}

output "ami_transfer_lambda_arn" {
  description = "Dedicated Lambda function ARN for manual AMI copy/export operations, or null when transfer is disabled."
  value       = local.ami_transfer_enabled ? aws_lambda_function.ami_transfer[0].arn : null
}

output "ami_transfer_copy_enabled" {
  description = "Whether manual AMI copy action is enabled in Lambda environment."
  value       = var.ami_transfer.enable_copy
}

output "ami_transfer_export_enabled" {
  description = "Whether manual AMI export action is enabled in Lambda environment."
  value       = var.ami_transfer.enable_export
}

output "ami_export_bucket_name" {
  description = "S3 bucket used by manual AMI export, or null when export is disabled."
  value       = var.ami_transfer.enable_export ? local.manual_export_bucket_name : null
}

output "scheduler_names" {
  description = "All EventBridge Scheduler names created."
  value       = [for s in aws_scheduler_schedule.this : s.name]
}

output "scheduler_arns" {
  description = "All EventBridge Scheduler ARNs created."
  value       = { for k, s in aws_scheduler_schedule.this : k => s.arn }
}

output "scheduler_invoke_role_arn" {
  description = "IAM role ARN used by EventBridge Scheduler to invoke Lambda."
  value       = aws_iam_role.scheduler_invoke.arn
}

output "scheduler_lambda_role_arn" {
  description = "IAM execution role ARN for scheduler Lambda."
  value       = aws_iam_role.lambda_exec.arn
}

output "ami_transfer_lambda_role_arn" {
  description = "IAM execution role ARN for AMI transfer Lambda, or null when transfer is disabled."
  value       = local.ami_transfer_enabled ? aws_iam_role.transfer_lambda_exec[0].arn : null
}

output "scheduler_dlq_arn" {
  description = "ARN of the scheduler Lambda dead-letter SQS queue."
  value       = aws_sqs_queue.lambda_dlq.arn
}

output "ami_transfer_dlq_arn" {
  description = "ARN of the AMI transfer Lambda dead-letter SQS queue, or null when transfer is disabled."
  value       = local.ami_transfer_enabled ? aws_sqs_queue.ami_transfer_dlq[0].arn : null
}

output "scheduler_log_group_name" {
  description = "CloudWatch log group name for scheduler Lambda."
  value       = aws_cloudwatch_log_group.scheduler.name
}

output "ami_transfer_log_group_name" {
  description = "CloudWatch log group name for AMI transfer Lambda, or null when transfer is disabled."
  value       = local.ami_transfer_enabled ? aws_cloudwatch_log_group.ami_transfer[0].name : null
}

output "workspace_log_group_name" {
  description = "CloudWatch log group name for EC2 bootstrap, cloud-init, and instance logs."
  value       = module.ec2_instance.cloudwatch_log_group_name
}

output "ssm_start_session_command" {
  description = "CLI command to start an SSM Session Manager shell."
  value       = var.enable_session_manager ? module.ec2_instance.ssm_start_session_command : null
}

output "manual_copy_latest_ami_permission_command" {
  description = "Permission command to allow AMI copy operation, or null when copy is disabled."
  value       = var.ami_transfer.enable_copy && contains(keys(aws_scheduler_schedule.this), "ami_weekly") ? "aws lambda add-permission --function-name ${aws_lambda_function.ami_transfer[0].function_name} --statement-id AllowExecutionFromScheduler --action lambda:InvokeFunction --principal scheduler.amazonaws.com --source-arn ${aws_scheduler_schedule.this["ami_weekly"].arn}" : null
}

output "manual_export_latest_ami_example" {
  description = "Example command to manually export the latest managed AMI to S3, or null when export is disabled."
  value       = var.ami_transfer.enable_export ? "aws lambda invoke --function-name ${aws_lambda_function.ami_transfer[0].function_name} --payload '{\"action\":\"export_latest_ami\",\"disk_format\":\"${var.ami_transfer.export_disk_format}\"}' export-latest-ami.json" : null
}

# ================== Cost Report Outputs ==================

output "cost_report" {
  description = "Cost report configuration and status."
  value       = var.cost_report
}

output "cost_report_enabled" {
  description = "Whether cost report is enabled."
  value       = local.cost_report_enabled
}
