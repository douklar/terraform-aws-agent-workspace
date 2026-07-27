# Plan-time tests for the root module's validations and preconditions.
#
# A mocked AWS provider lets these run with no credentials and no real API
# calls: every data source and resource returns generated values, so we can
# assert on variable validation and preconditions offline (in CI).

# Supplying subnet_id bypasses subnet auto-discovery, which returns an empty
# list under the mocked provider and would otherwise trip the instance's
# "no subnets found" precondition before we reach the checks under test.
variables {
  subnet_id = "subnet-0123456789abcdef0"
}

mock_provider "aws" {
  # Pin the identity/partition/region data sources to realistic values so the
  # ARNs the module builds pass the provider's ARN validation. Without this the
  # mock generates random strings and every IAM policy ARN is rejected.
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
    }
  }
  mock_data "aws_region" {
    defaults = {
      name = "eu-central-1"
    }
  }
  mock_data "aws_ami" {
    defaults = {
      id = "ami-0123456789abcdef0"
    }
  }
}

# The default configuration must produce a valid plan.
run "defaults_plan_cleanly" {
  command = plan
}

# encryption.type is constrained to three values.
run "rejects_invalid_encryption_type" {
  command = plan

  variables {
    encryption = { type = "bogus" }
  }

  expect_failures = [var.encryption]
}

# A KMS key ARN may only be supplied alongside customer-managed encryption.
run "rejects_kms_key_without_customer_managed" {
  command = plan

  variables {
    encryption = {
      type        = "aws-managed"
      kms_key_arn = "arn:aws:kms:eu-central-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"
    }
  }

  expect_failures = [var.encryption]
}

# A cross-midnight window (start_time > stop_time) cannot be enforced by the
# discrete start/stop crons alone, so disabling reconcile must fail the plan.
run "cross_midnight_requires_reconcile" {
  command = plan

  variables {
    scheduler_features = { reconcile = false }
    instance_schedule_windows = [{
      name       = "overnight"
      mode       = "free-time"
      timezone   = "UTC"
      days       = ["MON"]
      start_time = "22:00"
      stop_time  = "02:00"
    }]
  }

  expect_failures = [terraform_data.schedule_preconditions]
}

# The same window is valid when reconcile is enabled (the default), because the
# 15-minute reconcile job stops the instance at the correct time.
run "cross_midnight_ok_with_reconcile" {
  command = plan

  variables {
    instance_schedule_windows = [{
      name       = "overnight"
      mode       = "free-time"
      timezone   = "UTC"
      days       = ["MON"]
      start_time = "22:00"
      stop_time  = "02:00"
    }]
  }
}

# Customer-managed encryption with the module creating its own key, and both
# AMI-transfer actions plus cost reporting enabled, exercises the maximum
# resource fan-out: the created KMS key, its ARN threaded into every KMS-aware
# resource, and all count-gated IAM/S3/SQS/log-group resources in one plan.
run "customer_managed_full_fanout_plans" {
  command = plan

  variables {
    encryption = { type = "customer-managed" }
    ami_transfer = {
      enable_copy   = true
      enable_export = true
    }
    cost_report = {
      enabled         = true
      email_addresses = ["ops@example.com"]
    }
  }

  assert {
    condition     = length(aws_kms_key.this) == 1
    error_message = "customer-managed encryption without a supplied key must create exactly one KMS key"
  }

  assert {
    condition     = aws_sqs_queue.lambda_dlq.kms_master_key_id != null
    error_message = "the resolved customer-managed KMS ARN must be threaded into the scheduler DLQ"
  }
}

# Customer-managed encryption where the operator supplies an existing key: the
# module must not create a key of its own and must use the supplied ARN.
run "customer_managed_byo_key_plans" {
  command = plan

  variables {
    encryption = {
      type        = "customer-managed"
      kms_key_arn = "arn:aws:kms:eu-central-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab"
    }
  }

  assert {
    condition     = length(aws_kms_key.this) == 0
    error_message = "the module must not create a KMS key when one is supplied via encryption.kms_key_arn"
  }
}

# lambda/scheduler.py collapses "keep-on" to on-demand before matching a window
# to a cohort, so a cohort with that name can never be matched and the instance
# would run continuously instead of following the window. The variable
# validation's reserved list must therefore cover every Lambda-side alias.
run "rejects_window_mode_that_is_a_lambda_alias" {
  command = plan

  variables {
    instance_schedule_windows = [{
      name       = "always_up"
      mode       = "keep-on"
      timezone   = "UTC"
      days       = ["MON"]
      start_time = "09:00"
      stop_time  = "17:00"
    }]
  }

  expect_failures = [var.instance_schedule_windows]
}

# A scheduler_mode naming no existing cohort leaves the instance unmanaged:
# reconcile can't resolve a window and the start/stop schedules skip it as a
# mode mismatch, so it silently never stops.
run "rejects_scheduler_mode_with_no_matching_cohort" {
  command = plan

  variables {
    scheduler_mode = "office-hours"
  }

  expect_failures = [terraform_data.schedule_preconditions]
}

# A custom cohort is valid as long as a window actually defines it.
run "accepts_scheduler_mode_matching_a_custom_cohort" {
  command = plan

  variables {
    scheduler_mode = "office-hours"
    instance_schedule_windows = [{
      name       = "office"
      mode       = "office-hours"
      timezone   = "UTC"
      days       = ["MON", "TUE", "WED", "THU", "FRI"]
      start_time = "09:00"
      stop_time  = "17:00"
    }]
  }
}

# Reserved modes bypass window matching entirely, so they stay valid even when
# no window declares them.
run "accepts_reserved_scheduler_mode_without_matching_window" {
  command = plan

  variables {
    scheduler_mode = "on-demand"
  }
}

# "unencrypted" is the only setting that genuinely turns encryption off, and it
# does so in exactly two places AWS allows: the EBS root volume and the SQS
# dead-letter queues. Everything else stays on an AWS-managed key because AWS
# gives no way to disable it, so no customer key may be created or referenced.
run "unencrypted_disables_only_what_aws_permits" {
  command = plan

  variables {
    encryption = { type = "unencrypted" }
  }

  assert {
    condition     = length(aws_kms_key.this) == 0
    error_message = "unencrypted must not create a KMS key"
  }

  assert {
    condition     = aws_sqs_queue.lambda_dlq.sqs_managed_sse_enabled == false
    error_message = "unencrypted must actually disable SQS server-side encryption"
  }

  assert {
    condition     = aws_sqs_queue.lambda_dlq.kms_master_key_id == null
    error_message = "unencrypted must not reference a KMS key on the DLQ"
  }

  assert {
    condition     = aws_cloudwatch_log_group.scheduler.kms_key_id == null
    error_message = "unencrypted must leave log groups on the AWS-managed default"
  }
}

# The default. AWS-owned/managed keys everywhere, no customer key, but SQS SSE
# switched on — the opposite of the unencrypted case above.
run "aws_managed_is_the_default_and_enables_sqs_sse" {
  command = plan

  assert {
    condition     = length(aws_kms_key.this) == 0
    error_message = "aws-managed must not create a customer-managed KMS key"
  }

  assert {
    condition     = aws_sqs_queue.lambda_dlq.sqs_managed_sse_enabled == true
    error_message = "aws-managed must enable SQS managed server-side encryption"
  }

  assert {
    condition     = output.kms_key_arn == null
    error_message = "kms_key_arn output must be null unless encryption is customer-managed"
  }
}

# Every optional feature off must remove all of its resources, not leave idle
# Lambdas, queues, buckets, or log groups behind billing quietly.
run "disabled_features_create_no_resources" {
  command = plan

  variables {
    scheduler_features = { reconcile = false }
    ami_transfer       = { enable_copy = false, enable_export = false }
    cost_report        = { enabled = false }
  }

  assert {
    condition     = length(aws_lambda_function.ami_transfer) == 0
    error_message = "the AMI transfer Lambda must not exist when both transfer actions are disabled"
  }

  assert {
    condition     = length(aws_sqs_queue.ami_transfer_dlq) == 0
    error_message = "the AMI transfer DLQ must not exist when transfer is disabled"
  }

  assert {
    condition     = length(aws_s3_bucket.ami_export) == 0
    error_message = "the export bucket must not exist when export is disabled"
  }

  assert {
    condition     = length(aws_budgets_budget.monthly_cost_alert) == 0
    error_message = "no budget may be created when cost reporting is disabled"
  }
}

# A cost alert with no recipients would be created and never notify anyone.
run "cost_report_without_addresses_creates_no_budget" {
  command = plan

  variables {
    cost_report = { enabled = true, email_addresses = [] }
  }

  assert {
    condition     = length(aws_budgets_budget.monthly_cost_alert) == 0
    error_message = "a budget with no subscriber addresses must not be created"
  }
}

# Export to a bucket the module does not create requires the caller to name it;
# otherwise the Lambda would be handed an empty bucket name at runtime.
run "rejects_export_without_a_bucket_to_write_to" {
  command = plan

  variables {
    ami_transfer = {
      enable_export        = true
      create_export_bucket = false
      export_s3_bucket     = ""
    }
  }

  expect_failures = [var.ami_transfer]
}

# Each schedule window produces a start and a stop schedule; the optional
# feature schedules are additive on top. Reconcile alone means 2 + 1.
run "schedule_count_tracks_the_configured_windows" {
  command = plan

  variables {
    instance_schedule_windows = [{
      name       = "weekdays"
      mode       = "free-time"
      timezone   = "UTC"
      days       = ["MON", "TUE", "WED", "THU", "FRI"]
      start_time = "09:00"
      stop_time  = "17:00"
    }]
  }

  assert {
    condition     = length(aws_scheduler_schedule.this) == 3
    error_message = "one window with reconcile enabled must produce start, stop, and reconcile schedules"
  }

  assert {
    condition     = length(aws_lambda_permission.allow_scheduler) == length(aws_scheduler_schedule.this)
    error_message = "every schedule needs a matching Lambda invoke permission"
  }
}

# An ingress rule naming no source would open nothing and silently do nothing.
run "rejects_ingress_rule_without_a_source" {
  command = plan

  variables {
    ingress_ports = [{
      protocol = "tcp"
      port     = 22
    }]
  }

  expect_failures = [var.ingress_ports]
}
