# ─── VPC DISCOVERY ───────────────────────────────────────────────────────────
# Supports null, "default", or real vpc-id for maximum flexibility
data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_vpc" "default_with_fallback" {
  count   = local.use_default_vpc ? 1 : 0
  default = true
}

data "aws_subnets" "selected_in_vpc" {
  count = local.use_default_vpc ? 0 : 1
  filter {
    name   = "vpc-id"
    values = [var.vpc_id]
  }
}

data "aws_subnets" "default_in_vpc" {
  count = local.use_default_vpc ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default_with_fallback[0].id]
  }
}

# ─── LOCALS ───────────────────────────────────────────────────────────────────
locals {

  # ── VPC MODE ───────────────────────────────────────────────────────────
  # vpc_id = null       → use AWS default VPC automatically
  # vpc_id = "default"  → use AWS default VPC automatically
  # vpc_id = "vpc-xxxx" → use that exact VPC ID
  use_default_vpc = var.vpc_id == null || var.vpc_id == "default"

  # ── RESOLVED NETWORK IDS ───────────────────────────────────────────────
  selected_vpc_id = local.use_default_vpc ? data.aws_vpc.default_with_fallback[0].id : var.vpc_id

  selected_subnet_id = var.subnet_id != null ? var.subnet_id : (
    local.use_default_vpc
    ? data.aws_subnets.default_in_vpc[0].ids[0]
    : data.aws_subnets.selected_in_vpc[0].ids[0]
  )

  selected_security_group_ids = length(var.security_group_ids) > 0 ? var.security_group_ids : [
    aws_security_group.workspace[0].id
  ]

  # ── TAGS ───────────────────────────────────────────────────────────────
  common_tags = merge(var.tags, {
    ManagedBy = "Terraform"
    Project   = "Workspace"
  })

  # ── INSTANCE PROFILE / SSM ─────────────────────────────────────────────
  session_manager_enabled       = var.enable_session_manager
  cloudwatch_logs_enabled       = true
  module_managed_extra_env_vars = var.extra_env_vars
  parameter_access_enabled      = var.developer_config.enable_tailscale || length(local.module_managed_extra_env_vars) > 0 || length(keys(var.extra_env_var_parameter_names)) > 0
  instance_profile_enabled      = local.session_manager_enabled || local.cloudwatch_logs_enabled || local.parameter_access_enabled
  create_iam_profile            = var.iam_instance_profile_name == null && local.instance_profile_enabled

  # Build a list of "port-protocol" keys from what user already defined
  # Example: [{ port=443, protocol="tcp" }] → ["443-tcp"]
  user_egress_port_keys = [for r in var.egress_ports : "${r.port}-${lower(r.protocol)}"]

  # Check if user already has 443 tcp — avoid duplicate rule
  ssm_443_already_defined = contains(local.user_egress_port_keys, "443-tcp")

  # This list will be EMPTY if SSM is disabled OR user already has 443 tcp
  # This list will have ONE item if SSM is enabled AND 443 tcp is missing
  # Result: port 443 is ALWAYS present when SSM is enabled, no duplicates
  ssm_auto_egress_rules = local.instance_profile_enabled && !local.ssm_443_already_defined ? [
    {
      port        = 443
      protocol    = "tcp"
      description = "AUTO-INJECTED: HTTPS for SSM endpoints (ssm, ssmmessages, ec2messages)"
    }
  ] : []

  # ── AMI / IAM ──────────────────────────────────────────────────────────
  ubuntu_2404_lts_ami = {
    owner_id    = "099720109477"
    filter_name = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
  }

  uefi_boot_modes = ["uefi", "uefi-preferred"]

  selected_ami_id        = var.ami_id != null ? var.ami_id : data.aws_ami.ubuntu_2404_lts[0].id
  selected_ami_boot_mode = var.ami_id != null ? null : data.aws_ami.ubuntu_2404_lts[0].boot_mode

  iam_profile_name = var.iam_instance_profile_name != null ? var.iam_instance_profile_name : (
    local.create_iam_profile ? aws_iam_instance_profile.ssm[0].name : null
  )

  cloudwatch_log_group_name  = "/${var.name_prefix}/ec2/workspace"
  cw_agent_config_param_name = "/${var.name_prefix}/cloudwatch-agent-config"

  extra_env_var_ssm_params = merge(
    { for name, parameter in aws_ssm_parameter.extra_env_vars : name => parameter.name },
    var.extra_env_var_parameter_names
  )
  extra_env_var_ssm_arns = [
    for parameter_name in values(local.extra_env_var_ssm_params) :
    "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${trim(parameter_name, "/")}"
  ]
}

# ─── CLOUDWATCH LOGS ──────────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "workspace" {
  name              = local.cloudwatch_log_group_name
  retention_in_days = 7
  kms_key_id        = var.workspace_log_group_kms_key_id

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-workspace-logs"
  })
}

# ─── SECURITY GROUP ───────────────────────────────────────────────────────────
resource "aws_security_group" "workspace" {
  count       = length(var.security_group_ids) == 0 ? 1 : 0
  name        = "${var.name_prefix}-workspace-sg"
  description = "Security group for workspace instances"
  vpc_id      = local.selected_vpc_id

  # ── AUTO-INJECTED SSM EGRESS ──────────────────────────────────────────
  # Automatically adds port 443 TCP when AWS service access is needed
  # and the user has NOT already defined it in var.egress_ports.
  # This ensures SSM/CloudWatch setup works without extra SG configuration.
  dynamic "egress" {
    for_each = local.ssm_auto_egress_rules
    content {
      description = egress.value.description
      from_port   = lower(egress.value.protocol) == "all" ? 0 : egress.value.port
      to_port     = lower(egress.value.protocol) == "all" ? 0 : egress.value.port
      protocol    = lower(egress.value.protocol) == "all" ? "-1" : lower(egress.value.protocol)
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  # ── USER-DEFINED EGRESS RULES ─────────────────────────────────────────
  dynamic "egress" {
    for_each = var.egress_ports
    content {
      description = "Egress port ${egress.value.port} ${egress.value.protocol}"
      from_port   = lower(egress.value.protocol) == "all" ? 0 : egress.value.port
      to_port     = lower(egress.value.protocol) == "all" ? 0 : egress.value.port
      protocol    = lower(egress.value.protocol) == "all" ? "-1" : lower(egress.value.protocol)
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  # ── USER-DEFINED INGRESS RULES ────────────────────────────────────────
  dynamic "ingress" {
    for_each = var.ingress_ports
    content {
      description      = coalesce(ingress.value.description, "Ingress ${lower(ingress.value.protocol)}")
      from_port        = lower(ingress.value.protocol) == "all" ? 0 : coalesce(ingress.value.from_port, ingress.value.port)
      to_port          = lower(ingress.value.protocol) == "all" ? 0 : coalesce(ingress.value.to_port, ingress.value.from_port, ingress.value.port)
      protocol         = lower(ingress.value.protocol) == "all" ? "-1" : lower(ingress.value.protocol)
      cidr_blocks      = ingress.value.cidr_blocks
      ipv6_cidr_blocks = ingress.value.ipv6_cidr_blocks
      security_groups  = ingress.value.security_groups
      self             = ingress.value.self
    }
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-workspace-sg"
  })
}

# ─── SSM PARAMETERS ───────────────────────────────────────────────────────────
resource "aws_ssm_parameter" "tailscale_auth_key" {
  count       = var.developer_config.enable_tailscale ? 1 : 0
  name        = "/${var.name_prefix}/tailscale-auth-key"
  description = "Tailscale authentication key for workspace instance. Replace value in console after initial deployment."
  type        = "SecureString"
  key_id      = var.ssm_parameter_kms_key_id
  value       = "replace_with_auth_key"
  overwrite   = true

  lifecycle {
    ignore_changes = [value]
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-tailscale-auth-key"
  })
}

resource "aws_ssm_parameter" "extra_env_vars" {
  for_each = local.module_managed_extra_env_vars

  name        = "/${var.name_prefix}/extra-environment-variables/${each.key}"
  description = "Runtime environment variable ${each.key} for workspace instance. Replace value in SSM Parameter Store after initial deployment."
  type        = "SecureString"
  key_id      = var.ssm_parameter_kms_key_id
  value       = "replace_in_ssm_after_apply"
  overwrite   = true

  lifecycle {
    ignore_changes = [value]
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-${each.key}"
  })
}

resource "aws_ssm_parameter" "instance_name" {
  name        = "/${var.name_prefix}/instance-name"
  description = "Instance name for workspace"
  type        = "String"
  value       = var.instance_name
  overwrite   = true

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-instance-name"
  })
}

resource "aws_ssm_parameter" "cw_agent_config" {
  name        = local.cw_agent_config_param_name
  description = "CloudWatch Agent log collection config for the workspace instance."
  type        = "String"
  value = jsonencode({
    agent = {
      run_as_user = "root"
    }
    logs = {
      logs_collected = {
        files = {
          collect_list = [
            {
              file_path       = "/var/log/user-data-debug.log"
              log_group_name  = local.cloudwatch_log_group_name
              log_stream_name = "{instance_id}/user-data-debug"
            },
            {
              file_path       = "/var/log/cloud-init-output.log"
              log_group_name  = local.cloudwatch_log_group_name
              log_stream_name = "{instance_id}/cloud-init-output"
            },
            {
              file_path       = "/var/log/cloud-init.log"
              log_group_name  = local.cloudwatch_log_group_name
              log_stream_name = "{instance_id}/cloud-init"
            },
            {
              file_path       = "/var/log/syslog"
              log_group_name  = local.cloudwatch_log_group_name
              log_stream_name = "{instance_id}/syslog"
            }
          ]
        }
      }
    }
  })
  overwrite = true

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-cloudwatch-agent-config"
  })
}

# ─── IAM ROLES FOR SESSION MANAGER ────────────────────────────────────────────
resource "aws_iam_role" "ssm" {
  count = local.create_iam_profile ? 1 : 0
  name  = "${var.name_prefix}-ssm-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-ssm-role"
  })
}

resource "aws_iam_role_policy_attachment" "ssm_managed_core" {
  count      = local.create_iam_profile && local.session_manager_enabled ? 1 : 0
  role       = aws_iam_role.ssm[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ssm_parameters" {
  count = local.create_iam_profile && local.parameter_access_enabled ? 1 : 0
  name  = "${var.name_prefix}-ssm-parameters"
  role  = aws_iam_role.ssm[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        Resource = concat(
          aws_ssm_parameter.tailscale_auth_key[*].arn,
          local.extra_env_var_ssm_arns
        )
      }
    ]
  })
}

resource "aws_ssm_association" "cloudwatch_agent_package" {
  count = local.session_manager_enabled ? 1 : 0

  name = "AWS-ConfigureAWSPackage"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.workspace.id]
  }

  parameters = {
    action = "Install"
    name   = "AmazonCloudWatchAgent"
  }

  apply_only_at_cron_interval = false
}

resource "aws_ssm_association" "cloudwatch_agent_config" {
  count = local.session_manager_enabled ? 1 : 0

  name = "AmazonCloudWatch-ManageAgent"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.workspace.id]
  }

  parameters = {
    action                        = "configure"
    mode                          = "ec2"
    optionalConfigurationSource   = "ssm"
    optionalConfigurationLocation = aws_ssm_parameter.cw_agent_config.name
    optionalRestart               = "yes"
  }

  apply_only_at_cron_interval = false

  depends_on = [
    aws_ssm_association.cloudwatch_agent_package,
    aws_ssm_parameter.cw_agent_config
  ]
}

resource "aws_iam_role_policy" "cloudwatch_logs" {
  count = local.create_iam_profile && local.cloudwatch_logs_enabled ? 1 : 0
  name  = "${var.name_prefix}-cloudwatch-logs"
  role  = aws_iam_role.ssm[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:DescribeLogStreams",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.workspace.arn,
          "${aws_cloudwatch_log_group.workspace.arn}:*"
        ]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "ssm" {
  count = local.create_iam_profile ? 1 : 0
  name  = "${var.name_prefix}-ssm-profile"
  role  = aws_iam_role.ssm[0].name

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-ssm-profile"
  })
}

# ─── AMI DATA SOURCE ───────────────────────────────────────────────────────────
data "aws_ami" "ubuntu_2404_lts" {
  count = var.ami_id == null ? 1 : 0

  owners      = [local.ubuntu_2404_lts_ami.owner_id]
  most_recent = true

  filter {
    name   = "name"
    values = [local.ubuntu_2404_lts_ami.filter_name]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "boot-mode"
    values = local.uefi_boot_modes
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }

  filter {
    name   = "image-type"
    values = ["machine"]
  }
}

# ─── EC2 INSTANCE ─────────────────────────────────────────────────────────────
resource "aws_instance" "workspace" {
  depends_on = [
    aws_iam_role_policy_attachment.ssm_managed_core,
    aws_iam_role_policy.ssm_parameters,
    aws_iam_role_policy.cloudwatch_logs
  ]

  ami                         = local.selected_ami_id
  instance_type               = var.instance_type
  subnet_id                   = local.selected_subnet_id
  vpc_security_group_ids      = local.selected_security_group_ids
  associate_public_ip_address = var.associate_public_ip
  iam_instance_profile        = local.iam_profile_name
  user_data_replace_on_change = false
  metadata_options {
    http_tokens = "required"
  }
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region                = var.aws_region
    cloudwatch_log_group_name = local.cloudwatch_log_group_name
    enable_session_manager    = var.enable_session_manager
    enable_tailscale          = var.developer_config.enable_tailscale
    extra_env_var_ssm_params  = local.extra_env_var_ssm_params
    install_claude_code       = var.developer_config.install_claude_code
    install_codex_cli         = var.developer_config.install_codex_cli
    install_vscode            = var.developer_config.install_vscode
    instance_name             = var.instance_name
    name_prefix               = var.name_prefix
  })

  root_block_device {
    volume_size           = var.root_volume_size
    volume_type           = var.root_volume_type
    encrypted             = var.root_volume_encrypted
    kms_key_id            = var.root_volume_kms_key_id
    delete_on_termination = var.root_volume_delete_on_termination
  }

  tags = merge(local.common_tags, {
    Name = var.instance_name
  })

  volume_tags = local.common_tags
}

resource "aws_ec2_tag" "scheduler_mode" {
  resource_id = aws_instance.workspace.id
  key         = "scheduler"
  value       = "free-time"
}
