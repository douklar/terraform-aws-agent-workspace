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
  root_volume_encrypted             = var.encryption.type != "unencrypted"
  root_volume_kms_key_id            = local.resolved_kms_key_arn
  root_volume_delete_on_termination = var.storage.delete_on_termination
  developer_config                  = var.developer_config
  env_vars                          = var.env_vars
  enable_session_manager            = var.enable_session_manager
  scheduler_mode                    = var.scheduler_mode
  egress_ports                      = var.egress_ports
  ingress_ports                     = var.ingress_ports
  workspace_log_group_kms_key_id    = local.resolved_kms_key_arn
  ssm_parameter_kms_key_id          = local.resolved_kms_key_arn
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

  cost_report_enabled  = var.cost_report.enabled && length(var.cost_report.email_addresses) > 0
  ami_transfer_enabled = var.ami_transfer.enable_copy || var.ami_transfer.enable_export
  # Per-deployment value, so one workspace's cleanup never matches another's.
  manager_tag_value = "${var.name_prefix}-scheduler"

  # ── ENCRYPTION ─────────────────────────────────────────────────────────
  customer_managed_encryption = var.encryption.type == "customer-managed"
  # Only create a key when the user didn't bring their own.
  create_customer_kms_key = local.customer_managed_encryption && var.encryption.kms_key_arn == null
  resolved_kms_key_arn = local.customer_managed_encryption ? coalesce(
    var.encryption.kms_key_arn, try(aws_kms_key.this[0].arn, null)
  ) : null
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

  # Must match ON_DEMAND_ALIASES / DISABLED_ALIASES in lambda/scheduler.py.
  on_demand_scheduler_modes = ["on-demand", "ondemand", "always-on", "always", "on", "keep-on", "keep-running"]
  disabled_scheduler_modes  = ["disabled", "off", "manual", "paused", "ignore", "none", "false", "no"]
  reserved_scheduler_modes  = concat(local.on_demand_scheduler_modes, local.disabled_scheduler_modes)

  scheduler_window_modes    = distinct([for window in local.scheduler_allowed_windows : window.mode])
  normalized_scheduler_mode = lower(trimspace(var.scheduler_mode))

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

resource "aws_kms_key" "this" {
  # checkov:skip=CKV2_AWS_64:A key policy is explicitly defined below (root account delegation + the CloudWatch Logs service statement it requires) rather than left at the provider default.
  count = local.create_customer_kms_key ? 1 : 0

  description             = "Customer-managed key for ${var.name_prefix} agent workspace resources (EBS, SSM, SQS, S3, CloudWatch Logs, Lambda, EventBridge Scheduler)."
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "${var.name_prefix}-key-policy"
    Statement = [
      {
        Sid    = "EnableIamUserPermissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        # CloudWatch Logs needs this in the key policy, not just in IAM.
        Sid    = "AllowCloudWatchLogsToUseKey"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.aws_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            # The two Lambda log groups plus the EC2 workspace log group.
            "kms:EncryptionContext:aws:logs:arn" = [
              "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name_prefix}-*",
              "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/${var.name_prefix}/ec2/*"
            ]
          }
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-key"
  })
}

resource "aws_kms_alias" "this" {
  count = local.create_customer_kms_key ? 1 : 0

  name          = "alias/${var.name_prefix}-agent-workspace"
  target_key_id = aws_kms_key.this[0].key_id
}

resource "terraform_data" "schedule_preconditions" {
  lifecycle {
    # Only reconcile can enforce cross-midnight and all-day windows. The
    # start/stop crons fire the stop on the start's own day, which is wrong.
    precondition {
      condition = var.scheduler_features.reconcile || alltrue([
        for w in var.instance_schedule_windows :
        (tonumber(split(":", w.start_time)[0]) * 60 + tonumber(split(":", w.start_time)[1])) <
        (tonumber(split(":", w.stop_time)[0]) * 60 + tonumber(split(":", w.stop_time)[1]))
      ])
      error_message = "Cross-midnight or all-day instance_schedule_windows (start_time >= stop_time) require scheduler_features.reconcile = true, since reconcile is what stops the instance at the correct time for those windows. Enable reconcile, or split the window so every window has start_time < stop_time."
    }

    # A mode matching no cohort leaves the instance unmanaged, which only
    # shows up later as an instance that never stops.
    precondition {
      condition = contains(local.reserved_scheduler_modes, local.normalized_scheduler_mode) || contains(
        local.scheduler_window_modes, local.normalized_scheduler_mode
      )
      error_message = "scheduler_mode \"${var.scheduler_mode}\" matches no scheduling cohort. It must be a reserved mode (on-demand / disabled and their aliases) or the mode of one of the instance_schedule_windows, which currently define: ${join(", ", local.scheduler_window_modes)}."
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
        # Start/stop is scoped to the managed fleet: any instance carrying the
        # `scheduler` tag. This lets other instances opt in by tag while keeping
        # the Lambda from touching untagged instances in the account.
        Sid    = "ManageScheduledInstanceState"
        Effect = "Allow"
        Action = [
          "ec2:StartInstances",
          "ec2:StopInstances"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          Null = {
            "aws:ResourceTag/scheduler" = "false"
          }
        }
      },
      {
        Sid    = "CreateScheduledInstanceAmi"
        Effect = "Allow"
        Action = [
          "ec2:CreateImage"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          Null = {
            "aws:ResourceTag/scheduler" = "false"
          }
        }
      },
      {
        Sid    = "CreateScheduledInstanceImageArtifacts"
        Effect = "Allow"
        Action = [
          "ec2:CreateImage",
          "ec2:CreateSnapshot"
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}::image/*",
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}::snapshot/*",
          "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:volume/*"
        ]
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
        # Deletion is scoped to artifacts this scheduler created (across every
        # managed instance), never anything else in the account.
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
            "ec2:ResourceTag/CreatedBy" = local.manager_tag_value
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
        Sid    = "RunManagedPatchBaselineDocument"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunPatchBaseline"
      },
      {
        Sid    = "RunManagedPatchBaselineInstances"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          Null = {
            "aws:ResourceTag/scheduler" = "false"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "lambda_xray" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
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

resource "aws_iam_role_policy" "lambda_exec_kms" {
  # Customer-managed key only: an encrypted log group and DLQ need the caller
  # to hold KMS permissions, not just the key policy.
  count = local.customer_managed_encryption ? 1 : 0

  name = "${var.name_prefix}-scheduler-lambda-kms"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "UseCustomerManagedKeyViaLogsAndSqs"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = local.resolved_kms_key_arn
        Condition = {
          StringEquals = {
            "kms:ViaService" = [
              "logs.${var.aws_region}.amazonaws.com",
              "sqs.${var.aws_region}.amazonaws.com"
            ]
          }
        }
      },
      {
        # CreateImage/CreateSnapshot on an encrypted volume needs these on the
        # key as well as the ec2:* permissions granted below.
        Sid    = "UseCustomerManagedKeyForEbsBackups"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKeyWithoutPlaintext",
          "kms:ReEncrypt*"
        ]
        Resource = local.resolved_kms_key_arn
      },
      {
        Sid      = "CreateGrantForCustomerManagedKeyOnAwsResource"
        Effect   = "Allow"
        Action   = "kms:CreateGrant"
        Resource = local.resolved_kms_key_arn
        Condition = {
          Bool = {
            "kms:GrantIsForAWSResource" = "true"
          }
        }
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
  # checkov:skip=CKV_AWS_290:ec2:CopyImage cannot be scoped to the destination AMI, which does not exist until the copy runs.
  # checkov:skip=CKV_AWS_355:ec2:CopyImage has no resource-level permissions, so "*" is required by the AWS API.
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
      },
      {
        # CopyImage fails if tags are sent without CreateTags. CreateAction is
        # not supported here, so scope by resource and tag key instead.
        Sid    = "TagCopiedAmiArtifacts"
        Effect = "Allow"
        Action = [
          "ec2:CreateTags"
        ]
        Resource = [
          "arn:${data.aws_partition.current.partition}:ec2:*::image/*",
          "arn:${data.aws_partition.current.partition}:ec2:*::snapshot/*"
        ]
        Condition = {
          "ForAllValues:StringEquals" = {
            "aws:TagKeys" = [
              "CreatedBy",
              "BackupType",
              "SourceImageId"
            ]
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "transfer_lambda_export" {
  # checkov:skip=CKV_AWS_290:ec2:ExportImage/CreateTags/DescribeExportImageTasks have no resource-level permission support, so "*" is required by the AWS API.
  # checkov:skip=CKV_AWS_355:ec2:ExportImage/DescribeExportImageTasks have no resource-level permissions, so "*" is required by the AWS API.
  count = var.ami_transfer.enable_export ? 1 : 0

  name = "${var.name_prefix}-ami-transfer-lambda-export"
  role = aws_iam_role.transfer_lambda_exec[0].id

  # The vmimport role does the S3 writes, not this Lambda, so no S3
  # permissions belong here.
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
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "transfer_lambda_logs" {
  count = local.ami_transfer_enabled ? 1 : 0

  role       = aws_iam_role.transfer_lambda_exec[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "transfer_lambda_xray" {
  count = local.ami_transfer_enabled ? 1 : 0

  role       = aws_iam_role.transfer_lambda_exec[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
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

resource "aws_iam_role_policy" "transfer_lambda_kms" {
  # Needed only for a customer-managed key: same reasoning as lambda_exec_kms.
  count = local.ami_transfer_enabled && local.customer_managed_encryption ? 1 : 0

  name = "${var.name_prefix}-ami-transfer-lambda-kms"
  role = aws_iam_role.transfer_lambda_exec[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "UseCustomerManagedKeyViaLogsAndSqs"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = local.resolved_kms_key_arn
        Condition = {
          StringEquals = {
            "kms:ViaService" = [
              "logs.${var.aws_region}.amazonaws.com",
              "sqs.${var.aws_region}.amazonaws.com"
            ]
          }
        }
      },
      {
        # CopyImage with a KmsKeyId needs these on the key. See
        # copy_latest_ami in ami_transfer.py.
        Sid    = "UseCustomerManagedKeyForAmiCopy"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:GenerateDataKeyWithoutPlaintext",
          "kms:ReEncrypt*"
        ]
        Resource = local.resolved_kms_key_arn
      },
      {
        Sid      = "CreateGrantForCustomerManagedKeyOnAwsResource"
        Effect   = "Allow"
        Action   = "kms:CreateGrant"
        Resource = local.resolved_kms_key_arn
        Condition = {
          Bool = {
            "kms:GrantIsForAWSResource" = "true"
          }
        }
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
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
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

resource "aws_iam_role_policy" "scheduler_invoke_kms" {
  # Scheduler decrypts the target payload with this role at invoke time.
  count = local.customer_managed_encryption ? 1 : 0

  name = "${var.name_prefix}-scheduler-invoke-kms"
  role = aws_iam_role.scheduler_invoke.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DecryptSchedulePayload"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = local.resolved_kms_key_arn
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
  # checkov:skip=CKV_AWS_117:Function calls only public AWS APIs (EC2/SSM); it has no VPC resources to reach, so VPC attachment adds NAT cost without benefit.
  # checkov:skip=CKV_AWS_272:Code signing is unnecessary for this in-repo, archive-built function with a source_code_hash integrity check.
  # checkov:skip=CKV_AWS_173:Environment variables contain no secrets (instance ID, tag keys, schedule JSON); a customer-managed key is used when encryption.type is customer-managed, otherwise the AWS-managed Lambda key encrypts them at rest.
  # checkov:skip=CKV_AWS_115:Runs on one schedule for one instance, never concurrently. A fixed reservation breaks apply on accounts at the minimum unreserved-concurrency quota.
  depends_on = [
    aws_iam_role_policy.lambda_ec2,
    aws_iam_role_policy.lambda_dlq,
    aws_iam_role_policy.lambda_exec_kms,
    aws_iam_role_policy_attachment.lambda_logs,
    aws_iam_role_policy_attachment.lambda_xray,
    aws_cloudwatch_log_group.scheduler
  ]

  function_name                  = local.scheduler_lambda_name
  role                           = aws_iam_role.lambda_exec.arn
  runtime                        = "python3.14"
  handler                        = "scheduler.lambda_handler"
  filename                       = data.archive_file.scheduler_zip.output_path
  source_code_hash               = data.archive_file.scheduler_zip.output_base64sha256
  timeout                        = 360
  memory_size                    = 256
  reserved_concurrent_executions = null
  kms_key_arn                    = local.resolved_kms_key_arn

  tracing_config {
    mode = "Active"
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.lambda_dlq.arn
  }

  environment {
    variables = {
      DAILY_RETENTION_DAYS     = tostring(var.scheduler_features.daily_snapshot_retention_days)
      WEEKLY_RETENTION_DAYS    = tostring(var.scheduler_features.weekly_ami_retention_days)
      MONTHLY_RETENTION_DAYS   = tostring(var.scheduler_features.monthly_ami_retention_days)
      MANAGED_INSTANCE_ID      = module.ec2_instance.instance_id
      MANAGER_TAG_KEY          = "CreatedBy"
      MANAGER_TAG_VALUE        = local.manager_tag_value
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
  # checkov:skip=CKV_AWS_117:Function calls only public AWS APIs (EC2 copy/export); it has no VPC resources to reach, so VPC attachment adds NAT cost without benefit.
  # checkov:skip=CKV_AWS_272:Code signing is unnecessary for this in-repo, archive-built function with a source_code_hash integrity check.
  # checkov:skip=CKV_AWS_173:Environment variables contain no secrets (instance ID, tag keys, bucket/prefix names); a customer-managed key is used when encryption.type is customer-managed, otherwise the AWS-managed Lambda key encrypts them at rest.
  # checkov:skip=CKV_AWS_115:Runs on one schedule for one instance, never concurrently. A fixed reservation breaks apply on accounts at the minimum unreserved-concurrency quota.
  count = local.ami_transfer_enabled ? 1 : 0

  depends_on = [
    aws_iam_role_policy.transfer_lambda_dlq,
    aws_iam_role_policy.transfer_lambda_ec2,
    aws_iam_role_policy.transfer_lambda_copy,
    aws_iam_role_policy.transfer_lambda_export,
    aws_iam_role_policy.transfer_lambda_kms,
    aws_cloudwatch_log_group.ami_transfer
  ]

  function_name                  = local.ami_transfer_lambda_name
  role                           = aws_iam_role.transfer_lambda_exec[0].arn
  runtime                        = "python3.14"
  handler                        = "ami_transfer.lambda_handler"
  filename                       = data.archive_file.ami_transfer_zip[0].output_path
  source_code_hash               = data.archive_file.ami_transfer_zip[0].output_base64sha256
  timeout                        = 300
  memory_size                    = 256
  reserved_concurrent_executions = null
  kms_key_arn                    = local.resolved_kms_key_arn

  tracing_config {
    mode = "Active"
  }

  dead_letter_config {
    target_arn = aws_sqs_queue.ami_transfer_dlq[0].arn
  }

  environment {
    variables = {
      MANAGED_INSTANCE_ID = module.ec2_instance.instance_id
      MANAGER_TAG_KEY     = "CreatedBy"
      MANAGER_TAG_VALUE   = local.manager_tag_value
      ENABLE_AMI_COPY     = tostring(var.ami_transfer.enable_copy)
      ENABLE_AMI_EXPORT   = tostring(var.ami_transfer.enable_export)
      COPY_TARGET_REGION  = var.ami_transfer.copy_target_region != "" ? var.ami_transfer.copy_target_region : var.aws_region
      EXPORT_S3_BUCKET    = var.ami_transfer.enable_export ? local.manual_export_bucket_name : ""
      EXPORT_S3_PREFIX    = local.manual_export_s3_prefix
      EXPORT_DISK_FORMAT  = var.ami_transfer.export_disk_format
      VMIMPORT_ROLE_NAME  = var.ami_transfer.export_role_name
      KMS_KEY_ARN         = local.resolved_kms_key_arn != null ? local.resolved_kms_key_arn : ""
    }
  }

  tags = local.common_tags
}

# ================== Native AWS Budget Cost Alerts ==================
# Account-wide on purpose (no cost_filter). A tag filter needs a cost-allocation
# tag activated in Billing first, and until then it matches $0 and never alerts.
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

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = var.cost_report.alert_threshold_percent
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.cost_report.email_addresses
  }
}

# ================== S3 Resources ==================

resource "aws_s3_bucket" "ami_export" {
  # checkov:skip=CKV_AWS_18:Access logging would require a second log bucket; this is a private, encrypted, versioned staging bucket for short-lived AMI exports.
  # checkov:skip=CKV_AWS_144:Cross-region replication is unwarranted for ephemeral, re-creatable export artifacts.
  # checkov:skip=CKV2_AWS_62:Event notifications are not needed; exports are driven on demand by the AMI transfer Lambda.
  count  = local.manual_export_bucket_mode ? 1 : 0
  bucket = local.manual_export_bucket_name

  # false fails on a non-empty bucket; true tears it down regardless.
  force_destroy = var.ami_transfer.export_bucket_force_destroy

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-ami-export"
  })
}

resource "aws_s3_bucket_ownership_controls" "ami_export" {
  count  = local.manual_export_bucket_mode ? 1 : 0
  bucket = aws_s3_bucket.ami_export[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
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
      sse_algorithm     = local.customer_managed_encryption ? "aws:kms" : "AES256"
      kms_master_key_id = local.resolved_kms_key_arn
    }
    bucket_key_enabled = local.customer_managed_encryption
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

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_cloudwatch_log_group" "scheduler" {
  # checkov:skip=CKV_AWS_338:30-day retention is an intentional cost trade-off for an operational scheduler log; a year of retention is unnecessary.
  name              = local.scheduler_log_group_name
  retention_in_days = 30
  kms_key_id        = local.resolved_kms_key_arn
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "ami_transfer" {
  # checkov:skip=CKV_AWS_338:30-day retention is an intentional cost trade-off for an operational Lambda log; a year of retention is unnecessary.
  count = local.ami_transfer_enabled ? 1 : 0

  name              = local.ami_transfer_log_group_name
  retention_in_days = 30
  kms_key_id        = local.resolved_kms_key_arn
  tags              = local.common_tags
}

resource "aws_sqs_queue" "lambda_dlq" {
  name                      = "${var.name_prefix}-scheduler-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = local.resolved_kms_key_arn
  # SQS SSE can actually be turned off, unlike S3/Logs/Lambda/EventBridge.
  sqs_managed_sse_enabled = local.customer_managed_encryption ? null : var.encryption.type == "aws-managed"
  tags                    = local.common_tags
}

resource "aws_sqs_queue" "ami_transfer_dlq" {
  count                     = local.ami_transfer_enabled ? 1 : 0
  name                      = "${var.name_prefix}-ami-transfer-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = local.resolved_kms_key_arn
  # Unlike S3/CloudWatch/Lambda/EventBridge, SQS SSE is genuinely optional -
  # AWS lets you disable it entirely, so "unencrypted" here means actually
  # unencrypted, not just "AWS-managed key," matching the user's choice.
  sqs_managed_sse_enabled = local.customer_managed_encryption ? null : var.encryption.type == "aws-managed"
  tags                    = local.common_tags
}

# ================== Schedulers ==================

resource "aws_scheduler_schedule" "this" {
  # checkov:skip=CKV_AWS_297:The schedule input is non-sensitive (an action name); a customer-managed key is used when encryption.type is customer-managed, otherwise the AWS-owned key encrypts it.
  for_each = local.scheduler_definitions

  depends_on = [
    aws_iam_role_policy.scheduler_invoke_lambda,
    aws_iam_role_policy.scheduler_invoke_kms
  ]

  name                         = "${var.name_prefix}-${each.key}"
  schedule_expression          = each.value.expression
  schedule_expression_timezone = each.value.timezone
  kms_key_arn                  = local.resolved_kms_key_arn

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
