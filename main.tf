data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

module "ec2_instance" {
  source = "./modules/ec2-instance"

  name_prefix                       = var.name_prefix
  instance_name                     = var.instance_name
  aws_region                        = var.aws_region
  vpc_id                            = var.vpc_id
  instance_type                     = var.instance_type
  ami_id                            = var.ami_id
  subnet_id                         = var.subnet_id
  security_group_ids                = var.security_group_ids
  associate_public_ip               = var.associate_public_ip
  root_volume_size                  = var.storage.size_gb
  root_volume_type                  = "gp3"
  root_volume_encrypted             = var.storage.encrypted
  root_volume_kms_key_id            = var.kms_key_arn
  root_volume_delete_on_termination = var.storage.delete_on_termination
  developer_config                  = var.developer_config
  extra_env_vars                    = var.extra_env_vars
  extra_env_var_parameter_names     = var.extra_env_var_parameter_names
  enable_session_manager            = var.enable_session_manager
  egress_ports                      = var.egress_ports
  ingress_ports                     = var.ingress_ports
  workspace_log_group_kms_key_id    = var.kms_key_arn
  ssm_parameter_kms_key_id          = var.kms_key_arn
  tags                              = var.tags
}

locals {
  common_tags = merge(var.tags, {
    ManagedBy   = "Terraform"
    Project     = var.name_prefix
    Environment = var.environment
    CostCenter  = var.cost_center
    Owner       = var.owner_email
  })

  cost_report_enabled         = var.cost_report.enabled && length(var.cost_report.email_addresses) > 0
  ami_transfer_enabled        = var.ami_transfer.enable_copy || var.ami_transfer.enable_export
  manual_export_bucket_mode   = var.ami_transfer.enable_export && var.ami_transfer.create_export_bucket
  manual_export_bucket_name   = var.ami_transfer.export_s3_bucket != "" ? var.ami_transfer.export_s3_bucket : "${var.name_prefix}-ami-export-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  manual_export_s3_prefix     = trim(var.ami_transfer.export_s3_prefix, "/")
  scheduler_lambda_name       = "${var.name_prefix}-instance-scheduler"
  ami_transfer_lambda_name    = "${var.name_prefix}-ami-transfer"
  scheduler_log_group_name    = "/aws/lambda/${local.scheduler_lambda_name}"
  ami_transfer_log_group_name = "/aws/lambda/${local.ami_transfer_lambda_name}"

  scheduler_allowed_windows = [
    for window in var.instance_schedule_windows : merge(window, {
      days = [for day in window.days : upper(day)]
      mode = lower(window.mode)
    })
  ]

  maintenance_timezone = var.scheduler_features.maintenance_timezone

  instance_start_schedules = {
    for window in local.scheduler_allowed_windows : "${window.name}_start" => {
      expression = format("cron(%d %d ? * %s *)", tonumber(split(":", window.start_time)[1]), tonumber(split(":", window.start_time)[0]), join(",", window.days))
      timezone   = window.timezone
      input = {
        action         = "start"
        scheduler_mode = window.mode
      }
    }
  }

  instance_stop_schedules = {
    for window in local.scheduler_allowed_windows : "${window.name}_stop" => {
      expression = format("cron(%d %d ? * %s *)", tonumber(split(":", window.stop_time)[1]), tonumber(split(":", window.stop_time)[0]), join(",", window.days))
      timezone   = window.timezone
      input = {
        action         = "stop"
        scheduler_mode = window.mode
      }
    }
  }

  optional_scheduler_definitions = merge(
    var.scheduler_features.reconcile ? {
      schedule_reconcile = {
        expression = "cron(0/15 * * * ? *)"
        timezone   = "UTC"
        input = {
          action = "enforce_schedule"
        }
      }
    } : {},
    var.scheduler_features.weekly_amis ? {
      ami_weekly = {
        expression = "cron(30 20 ? * SUN *)"
        timezone   = local.maintenance_timezone
        input = {
          action      = "create_ami"
          backup_type = "weekly"
        }
      }
    } : {},
    var.scheduler_features.monthly_amis ? {
      ami_monthly_full = {
        expression = "cron(45 20 1 * ? *)"
        timezone   = local.maintenance_timezone
        input = {
          action      = "create_ami"
          backup_type = "monthly"
        }
      }
    } : {},
    var.scheduler_features.daily_snapshots ? {
      snapshot_daily = {
        expression = "cron(15 1 * * ? *)"
        timezone   = local.maintenance_timezone
        input = {
          action = "create_daily_snapshots"
        }
      }
    } : {},
    var.scheduler_features.backup_cleanup ? {
      ami_cleanup = {
        expression = "cron(30 1 * * ? *)"
        timezone   = local.maintenance_timezone
        input = {
          action = "cleanup_amis"
        }
      }
    } : {},
    var.scheduler_features.security_update ? {
      security_update = {
        expression = "cron(30 5 ? * SAT-SUN *)"
        timezone   = local.maintenance_timezone
        input = {
          action = "security_update"
        }
      }
    } : {}
  )

  scheduler_definitions = merge(local.instance_start_schedules, local.instance_stop_schedules, local.optional_scheduler_definitions)
}

resource "terraform_data" "manual_export_preconditions" {
  lifecycle {
    precondition {
      condition     = !var.ami_transfer.enable_export || var.ami_transfer.create_export_bucket || var.ami_transfer.export_s3_bucket != ""
      error_message = "ami_transfer.export_s3_bucket must be set when create_export_bucket is false and enable_export is true."
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name = "${var.name_prefix}-scheduler-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-scheduler-lambda-role"
  })
}

resource "aws_iam_role_policy" "lambda_ec2" {
  name = "${var.name_prefix}-scheduler-lambda-ec2"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ManageWorkspaceInstanceState"
        Effect = "Allow"
        Action = [
          "ec2:StartInstances",
          "ec2:StopInstances"
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${module.ec2_instance.instance_id}"
        ]
      },
      {
        Sid    = "CreateWorkspaceAmi"
        Effect = "Allow"
        Action = [
          "ec2:CreateImage"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${module.ec2_instance.instance_id}"
      },
      {
        Sid    = "CreateManagedWorkspaceSnapshots"
        Effect = "Allow"
        Action = [
          "ec2:CreateSnapshot"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:volume/${module.ec2_instance.root_volume_id}"
      },
      {
        Sid    = "TagBackupArtifactsOnCreate"
        Effect = "Allow"
        Action = [
          "ec2:CreateTags"
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}::image/ami-*",
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}::snapshot/snap-*"
        ]
        Condition = {
          StringEquals = {
            "ec2:CreateAction" = [
              "CreateImage",
              "CreateSnapshot"
            ]
          }
        }
      },
      {
        Sid    = "DeleteManagedBackupArtifacts"
        Effect = "Allow"
        Action = [
          "ec2:DeregisterImage",
          "ec2:DeleteSnapshot"
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}::image/ami-*",
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}::snapshot/snap-*"
        ]
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/CreatedBy"        = "workspace-scheduler"
            "ec2:ResourceTag/SourceInstanceId" = module.ec2_instance.instance_id
          }
        }
      },
      {
        Sid    = "DescribeSchedulerResources"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeImages",
          "ec2:DescribeSnapshots",
          "ssm:GetCommandInvocation"
        ]
        Resource = "*"
      },
      {
        Sid    = "RunManagedPatchBaseline"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand"
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunPatchBaseline",
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/${module.ec2_instance.instance_id}"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_dlq" {
  name = "${var.name_prefix}-scheduler-lambda-dlq"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage"
        ]
        Resource = aws_sqs_queue.lambda_dlq.arn
      }
    ]
  })
}

resource "aws_iam_role" "transfer_lambda_exec" {
  count = local.ami_transfer_enabled ? 1 : 0

  name = "${var.name_prefix}-ami-transfer-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-ami-transfer-lambda-role"
  })
}

resource "aws_iam_role_policy" "transfer_lambda_ec2" {
  count = local.ami_transfer_enabled ? 1 : 0

  name = "${var.name_prefix}-ami-transfer-lambda-ec2"
  role = aws_iam_role.transfer_lambda_exec[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DescribeManagedAmis"
        Effect = "Allow"
        Action = [
          "ec2:DescribeImages"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "transfer_lambda_copy" {
  count = var.ami_transfer.enable_copy ? 1 : 0

  name = "${var.name_prefix}-ami-transfer-lambda-copy"
  role = aws_iam_role.transfer_lambda_exec[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CopyManagedAmi"
        Effect = "Allow"
        Action = [
          "ec2:CopyImage"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "transfer_lambda_export" {
  count = var.ami_transfer.enable_export ? 1 : 0

  name = "${var.name_prefix}-ami-transfer-lambda-export"
  role = aws_iam_role.transfer_lambda_exec[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ExportManagedAmi"
        Effect = "Allow"
        Action = [
          "ec2:ExportImage",
          "ec2:DescribeExportImageTasks",
          "ec2:CreateTags"
        ]
        Resource = "*"
      },
      {
        Sid    = "PassVmImportRoleOnly"
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.ami_transfer.export_role_name}"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "vmie.amazonaws.com"
          }
        }
      },
      {
        Sid    = "ListConfiguredExportBucket"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
          "s3:GetBucketLocation"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:s3:::${local.manual_export_bucket_name}"
      },
      {
        Sid    = "WriteConfiguredExportPrefix"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:AbortMultipartUpload"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:s3:::${local.manual_export_bucket_name}/${local.manual_export_s3_prefix}/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "transfer_lambda_logs" {
  count = local.ami_transfer_enabled ? 1 : 0

  role       = aws_iam_role.transfer_lambda_exec[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "transfer_lambda_dlq" {
  count = local.ami_transfer_enabled ? 1 : 0

  name = "${var.name_prefix}-ami-transfer-lambda-dlq"
  role = aws_iam_role.transfer_lambda_exec[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage"
        ]
        Resource = aws_sqs_queue.ami_transfer_dlq[0].arn
      }
    ]
  })
}

resource "aws_iam_role" "scheduler_invoke" {
  name = "${var.name_prefix}-scheduler-invoke-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "scheduler.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-scheduler-invoke-role"
  })
}

resource "aws_iam_role_policy" "scheduler_invoke_lambda" {
  name = "${var.name_prefix}-scheduler-invoke-lambda"
  role = aws_iam_role.scheduler_invoke.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = [
          aws_lambda_function.scheduler.arn
        ]
      }
    ]
  })
}

data "archive_file" "scheduler_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/scheduler.py"
  output_path = "${path.module}/lambda/scheduler.zip"
}

data "archive_file" "ami_transfer_zip" {
  count = local.ami_transfer_enabled ? 1 : 0

  type        = "zip"
  source_file = "${path.module}/lambda/ami_transfer.py"
  output_path = "${path.module}/lambda/ami_transfer.zip"
}


resource "aws_lambda_function" "scheduler" {
  depends_on = [
    terraform_data.manual_export_preconditions,
    aws_iam_role_policy.lambda_dlq,
    aws_cloudwatch_log_group.scheduler
  ]

  function_name    = local.scheduler_lambda_name
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "scheduler.lambda_handler"
  filename         = data.archive_file.scheduler_zip.output_path
  source_code_hash = data.archive_file.scheduler_zip.output_base64sha256
  timeout          = 360

  dead_letter_config {
    target_arn = aws_sqs_queue.lambda_dlq.arn
  }

  environment {
    variables = {
      DAILY_RETENTION_DAYS     = tostring(var.scheduler_features.daily_snapshot_retention_days)
      MONTHLY_RETENTION_DAYS   = tostring(var.scheduler_features.monthly_ami_retention_days)
      MANAGED_INSTANCE_ID      = module.ec2_instance.instance_id
      MANAGER_TAG_KEY          = "CreatedBy"
      MANAGER_TAG_VALUE        = "workspace-scheduler"
      SCHEDULER_MODE_TAG_KEY   = "scheduler"
      SCHEDULER_MODE_DEFAULT   = "free-time"
      SCHEDULER_MODE_ON_DEMAND = "on-demand"
      SCHEDULE_TIMEZONE        = local.maintenance_timezone
      INSTANCE_ALLOWED_WINDOWS = jsonencode(local.scheduler_allowed_windows)
      DRY_RUN                  = "false"
    }
  }

  tags = local.common_tags
}

resource "aws_lambda_function" "ami_transfer" {
  count = local.ami_transfer_enabled ? 1 : 0

  depends_on = [
    terraform_data.manual_export_preconditions,
    aws_iam_role_policy.transfer_lambda_dlq,
    aws_iam_role_policy.transfer_lambda_ec2,
    aws_cloudwatch_log_group.ami_transfer
  ]

  function_name    = local.ami_transfer_lambda_name
  role             = aws_iam_role.transfer_lambda_exec[0].arn
  runtime          = "python3.12"
  handler          = "ami_transfer.lambda_handler"
  filename         = data.archive_file.ami_transfer_zip[0].output_path
  source_code_hash = data.archive_file.ami_transfer_zip[0].output_base64sha256
  timeout          = 300

  dead_letter_config {
    target_arn = aws_sqs_queue.ami_transfer_dlq[0].arn
  }

  environment {
    variables = {
      MANAGED_INSTANCE_ID = module.ec2_instance.instance_id
      MANAGER_TAG_KEY     = "CreatedBy"
      MANAGER_TAG_VALUE   = "workspace-scheduler"
      ENABLE_AMI_COPY     = tostring(var.ami_transfer.enable_copy)
      ENABLE_AMI_EXPORT   = tostring(var.ami_transfer.enable_export)
      COPY_TARGET_REGION  = var.ami_transfer.copy_target_region != "" ? var.ami_transfer.copy_target_region : var.aws_region
      EXPORT_S3_BUCKET    = var.ami_transfer.enable_export ? local.manual_export_bucket_name : ""
      EXPORT_S3_PREFIX    = local.manual_export_s3_prefix
      EXPORT_DISK_FORMAT  = var.ami_transfer.export_disk_format
      VMIMPORT_ROLE_NAME  = var.ami_transfer.export_role_name
    }
  }

  tags = local.common_tags
}

# ================== Native AWS Budget Cost Alerts ==================
resource "aws_budgets_budget" "monthly_cost_alert" {
  count        = local.cost_report_enabled ? 1 : 0
  name         = "${var.name_prefix}-monthly-cost-alert"
  budget_type  = "COST"
  limit_amount = tostring(var.cost_report.monthly_budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = var.cost_report.alert_threshold_percent
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.cost_report.email_addresses
  }
}

# ================== S3 Resources ==================

resource "aws_s3_bucket" "ami_export" {
  count  = local.manual_export_bucket_mode ? 1 : 0
  bucket = local.manual_export_bucket_name

  lifecycle {
    prevent_destroy = true
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-ami-export"
  })
}

resource "aws_s3_bucket_public_access_block" "ami_export" {
  count  = local.manual_export_bucket_mode ? 1 : 0
  bucket = aws_s3_bucket.ami_export[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "ami_export" {
  count  = local.manual_export_bucket_mode ? 1 : 0
  bucket = aws_s3_bucket.ami_export[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ami_export" {
  count  = local.manual_export_bucket_mode ? 1 : 0
  bucket = aws_s3_bucket.ami_export[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = var.kms_key_arn != null
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "ami_export" {
  count  = local.manual_export_bucket_mode ? 1 : 0
  bucket = aws_s3_bucket.ami_export[0].id

  rule {
    id     = "expire-export-artifacts"
    status = "Enabled"

    filter {}

    expiration {
      days = var.ami_transfer.export_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_cloudwatch_log_group" "scheduler" {
  name              = local.scheduler_log_group_name
  retention_in_days = 30
  kms_key_id        = var.kms_key_arn
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "ami_transfer" {
  count = local.ami_transfer_enabled ? 1 : 0

  name              = local.ami_transfer_log_group_name
  retention_in_days = 30
  kms_key_id        = var.kms_key_arn
  tags              = local.common_tags
}

resource "aws_sqs_queue" "lambda_dlq" {
  name                      = "${var.name_prefix}-scheduler-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = var.kms_key_arn
  sqs_managed_sse_enabled   = var.kms_key_arn == null ? true : null
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "ami_transfer_dlq" {
  count                     = local.ami_transfer_enabled ? 1 : 0
  name                      = "${var.name_prefix}-ami-transfer-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = var.kms_key_arn
  sqs_managed_sse_enabled   = var.kms_key_arn == null ? true : null
  tags                      = local.common_tags
}

# ================== Schedulers ==================

resource "aws_scheduler_schedule" "this" {
  for_each = local.scheduler_definitions

  name                         = "${var.name_prefix}-${each.key}"
  schedule_expression          = each.value.expression
  schedule_expression_timezone = each.value.timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.scheduler.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
    input    = jsonencode(each.value.input)
  }
}

resource "aws_lambda_permission" "allow_scheduler" {
  for_each = aws_scheduler_schedule.this

  statement_id  = "AllowExecutionFromScheduler-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = each.value.arn
}
