variable "name_prefix" {
  description = "Prefix for naming resources"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,23}$", var.name_prefix))
    error_message = "name_prefix must be 1-24 lowercase letters, numbers, or hyphens and start with a letter or number."
  }
}

variable "instance_name" {
  description = "Name tag for the EC2 instance"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]{0,62}$", var.instance_name))
    error_message = "instance_name must be 1-63 characters and contain only letters, numbers, and hyphens."
  }
}

variable "instance_type" {
  description = "UEFI-capable x86_64 Nitro EC2 instance type from a supported family"
  type        = string
  default     = "m7i-flex.large"

  validation {
    condition     = can(regex("^(t3|t3a|m5|m6i|m7i|m7i-flex|c5|c6i|c7i|r5|r6i|r7i)\\.[a-z0-9]+(\\.[a-z0-9]+)?$", var.instance_type))
    error_message = "Instance type must be a supported x86_64 Nitro family matching the README guidance: t3, t3a, m5, m6i, m7i, m7i-flex, c5, c6i, c7i, r5, r6i, or r7i. Example: m7i-flex.large."
  }
}

variable "vpc_id" {
  description = "VPC ID to deploy into. If null, uses the default VPC."
  type        = string
  default     = null

  validation {
    condition     = var.vpc_id == null || var.vpc_id == "default" || can(regex("^vpc-[0-9a-f]+$", var.vpc_id))
    error_message = "vpc_id must be null, default, or a valid VPC ID."
  }
}

variable "subnet_id" {
  description = "Subnet ID for the instance. If null, uses default VPC."
  type        = string
  default     = null

  validation {
    condition     = var.subnet_id == null || can(regex("^subnet-[0-9a-f]+$", var.subnet_id))
    error_message = "subnet_id must be null or a valid subnet ID."
  }
}

variable "security_group_ids" {
  description = "Security group IDs. If empty, creates a new SG with default rules."
  type        = list(string)
  default     = []
}

variable "egress_ports" {
  description = "List of egress port maps: { port = number, protocol = string }. Protocols: tcp, udp, all."
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

variable "root_volume_size" {
  description = "Root volume size in GB"
  type        = number
  default     = 30

  validation {
    condition     = var.root_volume_size >= 8 && var.root_volume_size <= 16384
    error_message = "Root volume size must be between 8 and 16384 GB."
  }
}

variable "root_volume_type" {
  description = "Root volume type (gp3, gp2, io2, etc.)"
  type        = string
  default     = "gp3"

  validation {
    condition     = contains(["gp2", "gp3", "io1", "io2", "io2ex", "standard"], var.root_volume_type)
    error_message = "Root volume type must be one of: gp2, gp3, io1, io2, io2ex, standard."
  }
}

variable "associate_public_ip" {
  description = "Whether to associate a public IP address"
  type        = bool
  default     = false
}

variable "ami_id" {
  description = "Optional explicit AMI ID for the workspace instance. When null, the module resolves the latest Ubuntu 24.04 LTS AMI."
  type        = string
  default     = null

  validation {
    condition     = var.ami_id == null || can(regex("^ami-[0-9a-f]+$", var.ami_id))
    error_message = "ami_id must be null or a valid AMI ID."
  }
}

variable "root_volume_encrypted" {
  description = "Encrypt the root EBS volume"
  type        = bool
  default     = true
}

variable "root_volume_kms_key_id" {
  description = "Optional KMS key ID or ARN for root EBS volume encryption"
  type        = string
  default     = null
}

variable "root_volume_delete_on_termination" {
  description = "Whether EC2 should delete the root EBS volume when the instance is terminated"
  type        = bool
  default     = false
}

variable "iam_instance_profile_name" {
  description = "Optional existing IAM instance profile name. If set, that profile must allow SSM Parameter Store access when configured and CloudWatch Logs writes to the module log group."
  type        = string
  default     = null
}

variable "aws_region" {
  description = "AWS region used for region-specific bootstrap URLs"
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}-[a-z-]+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be an AWS region identifier, for example eu-central-1."
  }
}

variable "developer_config" {
  description = "Developer tooling installed or configured during instance bootstrap."
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
  description = "Enable AWS Systems Manager Session Manager for instance access"
  type        = bool
  default     = true
}

variable "scheduler_mode" {
  description = "Initial value for the instance's `scheduler` tag, which drives the scheduler Lambda. Use \"free-time\" to follow the configured schedule windows, or \"on-demand\" to keep the instance always on. This sets the value only at creation; change the `scheduler` tag at runtime (console, CLI, or Lambda) to override, and Terraform will not revert it."
  type        = string
  default     = "free-time"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.scheduler_mode))
    error_message = "scheduler_mode must be a lowercase identifier such as free-time or on-demand."
  }
}

variable "tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "workspace_log_group_kms_key_id" {
  description = "Optional KMS key ID or ARN for the EC2 workspace CloudWatch log group"
  type        = string
  default     = null
}

variable "ssm_parameter_kms_key_id" {
  description = "Optional KMS key ID or ARN for SecureString parameters created by the module"
  type        = string
  default     = null
}
