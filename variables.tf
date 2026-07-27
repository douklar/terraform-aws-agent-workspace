variable "aws_region" {
  description = "AWS region to deploy resources into."
  type        = string
  default     = "eu-central-1"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z-]+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be an AWS region identifier, for example eu-central-1."
  }
}

variable "instance_name" {
  description = "Name tag for the managed EC2 instance."
  type        = string
  default     = "workspace-ec2"
  nullable    = false

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]{0,62}$", var.instance_name))
    error_message = "instance_name must be 1-63 characters and contain only letters, numbers, and hyphens."
  }
}

variable "name_prefix" {
  description = "Prefix used by scheduler and IAM resources."
  type        = string
  default     = "workspace"
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,23}$", var.name_prefix))
    error_message = "name_prefix must be 1-24 lowercase letters, numbers, or hyphens and start with a letter or number."
  }
}

variable "instance_type" {
  description = "UEFI-capable x86_64 Nitro EC2 instance type from a supported family."
  type        = string
  default     = "m7i-flex.large"
  nullable    = false

  validation {
    condition     = can(regex("^(t3|t3a|m5|m6i|m7i|m7i-flex|c5|c6i|c7i|r5|r6i|r7i)\\.[a-z0-9]+(\\.[a-z0-9]+)?$", var.instance_type))
    error_message = "Instance type must be a supported x86_64 Nitro family matching the README guidance: t3, t3a, m5, m6i, m7i, m7i-flex, c5, c6i, c7i, r5, r6i, or r7i. Example: m7i-flex.large."
  }
}

variable "subnet_id" {
  description = "Optional subnet override. If null, default VPC first subnet is used."
  type        = string
  default     = null

  validation {
    condition     = var.subnet_id == null || can(regex("^subnet-[0-9a-f]+$", var.subnet_id))
    error_message = "subnet_id must be null or a valid subnet ID."
  }
}

variable "vpc_id" {
  description = "Optional VPC override. Use null or default to use the account default VPC."
  type        = string
  default     = null

  validation {
    condition     = var.vpc_id == null || var.vpc_id == "default" || can(regex("^vpc-[0-9a-f]+$", var.vpc_id))
    error_message = "vpc_id must be null, default, or a valid VPC ID."
  }
}

variable "security_group_ids" {
  description = "Optional security groups override. If empty, the EC2 submodule creates a security group."
  type        = list(string)
  default     = []
}

variable "egress_ports" {
  description = "List of egress port maps: { port, protocol }. Protocols: tcp, udp, all."
  type = list(object({
    port     = number
    protocol = string
  }))
  default = [
    { port = 443, protocol = "tcp" },
    { port = 80, protocol = "tcp" },
    { port = 41641, protocol = "udp" }
  ]

  validation {
    condition     = alltrue([for r in var.egress_ports : contains(["tcp", "udp", "all"], lower(r.protocol))])
    error_message = "Each egress protocol must be one of: tcp, udp, all."
  }

  validation {
    condition = alltrue([
      for r in var.egress_ports : lower(r.protocol) == "all" ? r.port == 0 : r.port >= 1 && r.port <= 65535
    ])
    error_message = "Each egress port must be 1-65535 for tcp/udp, or 0 when protocol is all."
  }
}

variable "ingress_ports" {
  description = "Generated security group ingress rules. Each rule must set protocol plus port or from_port/to_port for tcp/udp, and at least one source."
  type = list(object({
    protocol         = string
    port             = optional(number)
    from_port        = optional(number)
    to_port          = optional(number)
    description      = optional(string)
    cidr_blocks      = optional(list(string), [])
    ipv6_cidr_blocks = optional(list(string), [])
    security_groups  = optional(list(string), [])
    self             = optional(bool, false)
  }))
  default = []

  validation {
    condition     = alltrue([for r in var.ingress_ports : contains(["tcp", "udp", "all"], lower(r.protocol))])
    error_message = "Each ingress protocol must be one of: tcp, udp, all."
  }

  validation {
    condition = alltrue([
      for r in var.ingress_ports :
      lower(r.protocol) == "all" ? true : (
        try(r.port, null) != null || try(r.from_port, null) != null
      )
    ])
    error_message = "Each tcp/udp ingress rule must set port or from_port."
  }

  validation {
    condition = alltrue([
      for r in var.ingress_ports :
      lower(r.protocol) == "all" ? true : (
        (try(r.port, null) == null ? true : r.port >= 1 && r.port <= 65535) &&
        (try(r.from_port, null) == null ? true : r.from_port >= 1 && r.from_port <= 65535) &&
        (try(r.to_port, null) == null ? true : r.to_port >= 1 && r.to_port <= 65535) &&
        ((try(r.from_port, null) == null || try(r.to_port, null) == null) ? true : r.from_port <= r.to_port)
      )
    ])
    error_message = "Ingress ports must be in the range 1-65535, and from_port must be <= to_port when both are set."
  }

  validation {
    condition = alltrue([
      for r in var.ingress_ports :
      length(try(r.cidr_blocks, [])) + length(try(r.ipv6_cidr_blocks, [])) + length(try(r.security_groups, [])) + (try(r.self, false) ? 1 : 0) > 0
    ])
    error_message = "Each ingress rule must set at least one source: cidr_blocks, ipv6_cidr_blocks, security_groups, or self."
  }
}

variable "storage" {
  description = "Workspace disk settings. Most users only ever change size_gb. Encryption is controlled by var.encryption, not here."
  type = object({
    size_gb               = optional(number, 30)
    delete_on_termination = optional(bool, false)
  })
  default = {}

  validation {
    condition     = var.storage.size_gb >= 8 && var.storage.size_gb <= 16384
    error_message = "storage.size_gb must be between 8 and 16384."
  }
}

variable "associate_public_ip" {
  description = "Whether to associate a public IPv4 address with the workspace instance. Defaults to false for public modules; set true only for explicit public networking."
  type        = bool
  default     = false
}

variable "ami_id" {
  description = "Optional explicit AMI ID for the workspace instance. When null, the module resolves the latest Ubuntu 24.04 LTS AMI. Pin this for production stability."
  type        = string
  default     = null

  validation {
    condition     = var.ami_id == null || can(regex("^ami-[0-9a-f]+$", var.ami_id))
    error_message = "ami_id must be null or a valid AMI ID."
  }
}

variable "instance_schedule_windows" {
  description = "Allowed instance run windows. Times use 24-hour HH:MM format in each window timezone, for example 18:30. A window may cross midnight (e.g. start_time 22:00, stop_time 02:00); an equal start_time/stop_time means the window is active all day. Each window belongs to a scheduling cohort named by its `mode`; an instance follows a cohort by setting its `scheduler` tag to that mode (default cohort: free-time). Define windows with different modes to run different instances on different schedules."
  type = list(object({
    name       = string
    mode       = optional(string, "free-time")
    timezone   = string
    days       = list(string)
    start_time = string
    stop_time  = string
  }))
  default = [
    {
      name       = "free_time"
      mode       = "free-time"
      timezone   = "Europe/Berlin"
      days       = ["MON", "TUE", "WED", "THU", "FRI"]
      start_time = "18:30"
      stop_time  = "23:00"
    },
    {
      name       = "weekends"
      mode       = "free-time"
      timezone   = "Europe/Berlin"
      days       = ["SAT", "SUN"]
      start_time = "11:00"
      stop_time  = "22:00"
    }
  ]

  validation {
    condition     = length(var.instance_schedule_windows) > 0
    error_message = "instance_schedule_windows must contain at least one schedule window."
  }

  validation {
    condition     = length(distinct([for window in var.instance_schedule_windows : window.name])) == length(var.instance_schedule_windows)
    error_message = "Each instance_schedule_windows entry must have a unique name."
  }

  validation {
    condition     = alltrue([for window in var.instance_schedule_windows : can(regex("^[a-zA-Z0-9_-]{1,24}$", window.name))])
    error_message = "Each instance_schedule_windows name must be 1-24 characters and may only contain letters, numbers, underscores, and hyphens."
  }

  validation {
    # mode names a scheduling cohort. Tag an instance with scheduler=<mode> to
    # make it follow the windows of that mode. Any lowercase identifier is a
    # valid custom cohort (e.g. free-time, office-hours, night-shift), except the
    # reserved modes on-demand (always on) and disabled (scheduler ignores it).
    #
    # Must match ON_DEMAND_ALIASES / DISABLED_ALIASES in lambda/scheduler.py.
    condition = alltrue([
      for window in var.instance_schedule_windows :
      can(regex("^[a-z][a-z0-9-]*$", lower(window.mode))) &&
      !contains(
        [
          "on-demand", "ondemand", "always-on", "always", "on", "keep-on", "keep-running",
          "disabled", "off", "manual", "paused", "ignore", "none", "false", "no",
        ],
        lower(window.mode)
      )
    ])
    error_message = "Each instance_schedule_windows mode must be a lowercase identifier such as free-time, office-hours, or night-shift, and must not be a reserved mode. Reserved (always-on): on-demand, ondemand, always-on, always, on, keep-on, keep-running. Reserved (ignored by the scheduler): disabled, off, manual, paused, ignore, none, false, no."
  }

  validation {
    condition     = alltrue([for window in var.instance_schedule_windows : length(trimspace(window.timezone)) > 0])
    error_message = "Each instance_schedule_windows entry must set timezone, for example Europe/Rome or America/New_York."
  }

  validation {
    condition = alltrue([
      for window in var.instance_schedule_windows :
      length(regexall("^([01][0-9]|2[0-3]):[0-5][0-9]$", window.start_time)) > 0 &&
      length(regexall("^([01][0-9]|2[0-3]):[0-5][0-9]$", window.stop_time)) > 0
    ])
    error_message = "Each instance_schedule_windows start_time and stop_time must use 24-hour HH:MM format, for example 18:30."
  }

  validation {
    condition     = alltrue([for window in var.instance_schedule_windows : length(window.days) > 0])
    error_message = "Each instance_schedule_windows entry must include at least one day."
  }

  validation {
    condition = alltrue(flatten([
      for window in var.instance_schedule_windows : [
        for day in window.days : contains(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"], upper(day))
      ]
    ]))
    error_message = "Each instance_schedule_windows day must be one of MON, TUE, WED, THU, FRI, SAT, or SUN."
  }
}


variable "tags" {
  description = "Additional tags to apply to created resources."
  type        = map(string)
  default     = {}
}

variable "developer_config" {
  description = "Developer tooling installed or configured during EC2 bootstrap."
  type = object({
    install_vscode      = optional(bool, true)
    enable_tailscale    = optional(bool, true)
    install_claude_code = optional(bool, true)
    install_codex_cli   = optional(bool, false)
  })
  default = {}
}

variable "env_vars" {
  description = "Map of uppercase environment variable names to SSM SecureString parameter paths. Set a key's value to null to have the module create a placeholder SSM parameter (populate the value in SSM after apply). Set a key's value to an existing SSM parameter path to reference it directly without creating a new parameter."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for name in keys(var.env_vars) : can(regex("^[A-Z_][A-Z0-9_]*$", name))])
    error_message = "env_vars keys must be uppercase shell environment variable names, for example API_KEY."
  }

  validation {
    condition = alltrue([
      for parameter_name in values(var.env_vars) :
      parameter_name == null || (can(regex("^/?[A-Za-z0-9_./-]+$", parameter_name)) && !strcontains(parameter_name, "//") && !strcontains(parameter_name, ".."))
    ])
    error_message = "env_vars values must be null or valid SSM parameter paths without empty path segments or '..'."
  }
}

variable "enable_session_manager" {
  description = "Enable AWS Systems Manager Session Manager for instance access."
  type        = bool
  default     = true
}

variable "scheduler_mode" {
  description = "Initial scheduling mode for the instance. Use \"free-time\" to follow instance_schedule_windows (the instance is started/stopped on schedule), or \"on-demand\" to keep it always on. This sets the instance's `scheduler` tag at creation only; change that tag at runtime (console, CLI, or the scheduler Lambda) to override, and Terraform will not revert it."
  type        = string
  default     = "free-time"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.scheduler_mode))
    error_message = "scheduler_mode must be a lowercase identifier such as free-time or on-demand."
  }
}

variable "ami_transfer" {
  description = "Settings for manually copying or exporting the latest workspace AMI (disk image). Both actions are off by default. Enable copy to duplicate the AMI within or across AWS regions. Enable export to download it as a VMDK/VHD/RAW file to an S3 bucket. export_bucket_force_destroy controls whether `terraform destroy` (or disabling export) deletes a module-created export bucket that still holds objects: false (default) makes Terraform refuse to delete a non-empty export bucket, so you never lose exports you meant to keep; set it to true if you want a clean teardown regardless of bucket contents (for example a disposable/CI workspace). Only applies when create_export_bucket is true."
  type = object({
    enable_copy                 = optional(bool, false)
    copy_target_region          = optional(string, "")
    enable_export               = optional(bool, false)
    export_s3_bucket            = optional(string, "")
    create_export_bucket        = optional(bool, true)
    export_bucket_force_destroy = optional(bool, false)
    export_s3_prefix            = optional(string, "ami-exports")
    export_disk_format          = optional(string, "VMDK")
    export_retention_days       = optional(number, 30)
    export_role_name            = optional(string, "vmimport")
  })
  default = {}

  validation {
    condition     = !var.ami_transfer.enable_export || var.ami_transfer.create_export_bucket || var.ami_transfer.export_s3_bucket != ""
    error_message = "ami_transfer.export_s3_bucket must be set when create_export_bucket is false and enable_export is true."
  }

  validation {
    condition     = contains(["VMDK", "VHD", "RAW"], upper(var.ami_transfer.export_disk_format))
    error_message = "ami_transfer.export_disk_format must be one of: VMDK, VHD, RAW."
  }

  validation {
    condition     = var.ami_transfer.export_retention_days >= 1
    error_message = "ami_transfer.export_retention_days must be at least 1."
  }

  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.ami_transfer.export_role_name))
    error_message = "ami_transfer.export_role_name must be a valid IAM role name."
  }

  validation {
    condition     = length(trim(trimspace(var.ami_transfer.export_s3_prefix), "/")) > 0 && !strcontains(var.ami_transfer.export_s3_prefix, "..")
    error_message = "ami_transfer.export_s3_prefix must be a non-empty path and must not contain '..'."
  }

  validation {
    condition     = var.ami_transfer.copy_target_region == "" || can(regex("^[a-z]{2}-[a-z-]+-[0-9]+$", var.ami_transfer.copy_target_region))
    error_message = "ami_transfer.copy_target_region must be empty or a valid AWS region, for example us-east-1."
  }
}

variable "scheduler_features" {
  description = "Optional automated jobs that run on a schedule. All default to off. Each backup kind has its own retention: daily_snapshot_retention_days (7) for daily EBS snapshots, weekly_ami_retention_days (30) for weekly AMIs, and monthly_ami_retention_days (30) for monthly AMIs. Retention only takes effect when backup_cleanup is enabled; without it nothing is ever deleted."
  type = object({
    reconcile                     = optional(bool, true)
    daily_snapshots               = optional(bool, false)
    weekly_amis                   = optional(bool, false)
    monthly_amis                  = optional(bool, false)
    backup_cleanup                = optional(bool, false)
    security_update               = optional(bool, false)
    maintenance_timezone          = optional(string, "Europe/Berlin")
    daily_snapshot_retention_days = optional(number, 7)
    weekly_ami_retention_days     = optional(number, 30)
    monthly_ami_retention_days    = optional(number, 30)
  })
  default = {}

  validation {
    condition     = var.scheduler_features.daily_snapshot_retention_days >= 1
    error_message = "scheduler_features.daily_snapshot_retention_days must be at least 1."
  }

  validation {
    condition     = var.scheduler_features.weekly_ami_retention_days >= 1
    error_message = "scheduler_features.weekly_ami_retention_days must be at least 1."
  }

  validation {
    condition     = var.scheduler_features.monthly_ami_retention_days >= 1
    error_message = "scheduler_features.monthly_ami_retention_days must be at least 1."
  }
}

variable "encryption" {
  description = <<-EOT
    How to encrypt the resources this module manages.

      aws-managed      (default) AWS-owned/managed keys everywhere. No extra cost.
      customer-managed One KMS key for everything. Set kms_key_arn to reuse an
                       existing key, or leave it null and the module creates one.
      unencrypted      Turns encryption off where AWS allows it: the EBS root
                       volume and the SQS dead-letter queues.

    Everywhere else AWS encrypts unconditionally (S3, CloudWatch Logs, Lambda,
    EventBridge Scheduler) or the data is secret by design (SSM SecureString).
    Those stay on their AWS-managed default rather than a customer key.
  EOT
  type = object({
    type        = optional(string, "aws-managed")
    kms_key_arn = optional(string, null)
  })
  default = {}

  validation {
    condition     = contains(["unencrypted", "aws-managed", "customer-managed"], var.encryption.type)
    error_message = "encryption.type must be one of: unencrypted, aws-managed, customer-managed."
  }

  validation {
    condition     = var.encryption.kms_key_arn == null || var.encryption.type == "customer-managed"
    error_message = "encryption.kms_key_arn can only be set when encryption.type is customer-managed."
  }

  validation {
    condition     = var.encryption.kms_key_arn == null || can(regex("^arn:[a-z0-9-]+:kms:[a-z0-9-]+:[0-9]{12}:key/(mrk-)?[a-f0-9-]+$", var.encryption.kms_key_arn))
    error_message = "encryption.kms_key_arn must be null or a valid KMS key ARN, for example arn:aws:kms:eu-central-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab or a multi-region key arn:aws:kms:eu-central-1:123456789012:key/mrk-1234abcd1234abcd."
  }
}

variable "environment" {
  description = "Environment tag used for ops and cost reporting (for example: dev, staging, prod)."
  type        = string
  default     = "dev"
  nullable    = false
}

variable "cost_center" {
  description = "Cost center tag value for billing attribution."
  type        = string
  default     = "engineering"
  nullable    = false
}

variable "owner_email" {
  description = "Owner tag value used to identify operational responsibility."
  type        = string
  default     = "owner@example.com"
  nullable    = false
}

variable "cost_report" {
  description = "Native AWS Budgets cost alert configuration. This replaces custom Cost Explorer Lambda reporting."
  type = object({
    enabled                  = optional(bool, false)
    email_addresses          = optional(list(string), [])
    monthly_budget_limit_usd = optional(number, 100)
    alert_threshold_percent  = optional(number, 80)
  })
  default = {}

  validation {
    condition     = var.cost_report.monthly_budget_limit_usd > 0
    error_message = "cost_report.monthly_budget_limit_usd must be greater than 0."
  }

  validation {
    condition     = var.cost_report.alert_threshold_percent > 0 && var.cost_report.alert_threshold_percent <= 1000
    error_message = "cost_report.alert_threshold_percent must be > 0 and <= 1000."
  }
}
