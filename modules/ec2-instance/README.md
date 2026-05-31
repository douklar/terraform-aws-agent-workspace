# Harbor EC2 Instance Module

This submodule provisions one Ubuntu EC2 developer workspace with CloudWatch logging, optional Session Manager access, optional Tailscale bootstrap, and optional SSM-backed runtime environment variables. Most users should consume the repository root module instead.

## Usage

Configure providers in your root Terraform configuration:

```hcl
terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "eu-central-1"
}

module "workspace_ec2" {
  source = "git::https://github.com/douklar/harbor.git//modules/ec2-instance?ref=v1.0.0"

  aws_region    = "eu-central-1"
  name_prefix   = "harbor"
  instance_name = "harbor-workspace"

  associate_public_ip = false
}
```

## Network Defaults

- `associate_public_ip = false` by default.
- `ingress_ports = []` by default.
- Generated ingress rules must explicitly set at least one source: `cidr_blocks`, `ipv6_cidr_blocks`, `security_groups`, or `self`.
- If `security_group_ids` is not empty, the module attaches those groups and does not manage their rules.
- HTTPS egress on `443/tcp` is injected when CloudWatch logging, Session Manager, Tailscale, or configured SSM parameters need AWS service access.

The module does not create NAT. Bootstrap installs packages and optional tools from the internet, so `associate_public_ip = false` requires a subnet with NAT/proxy egress. For a default VPC with no NAT Gateway, set `associate_public_ip = true` explicitly. Tailscale also needs outbound internet access, but it does not need inbound security-group rules.

Example ingress rule:

```hcl
ingress_ports = [
  {
    protocol    = "tcp"
    port        = 8443
    cidr_blocks = ["203.0.113.10/32"]
  }
]
```

## AMI And Root Volume Safety

`ami_id` can pin a tested AMI. When null, the module resolves the latest official Canonical Ubuntu 24.04 LTS x86_64 gp3 AMI.

The module sets `user_data_replace_on_change = false`, defaults `root_volume_delete_on_termination = false`, and defaults `root_volume_encrypted = true`. AMI changes and other EC2 replacement-forcing arguments can still replace the instance. Retained root volumes may require manual recovery or cleanup.

## Session Manager Semantics

`enable_session_manager = true` installs/enables the SSM Agent and attaches `AmazonSSMManagedInstanceCore` when the module creates the instance profile.

`enable_session_manager = false` does not install/start the SSM Agent for Session Manager and does not attach `AmazonSSMManagedInstanceCore`. CloudWatch logging and SSM Parameter Store reads can still use the instance profile without granting Session Manager shell access.

If `iam_instance_profile_name` is supplied, the caller-managed profile must provide any needed CloudWatch Logs, SSM Parameter Store, Tailscale, and Session Manager permissions.

## Runtime Secrets

Use `extra_env_vars` when you want the module to create SSM SecureString placeholders for runtime tokens used by MCP servers, LLM CLIs, or other developer tools:

```hcl
extra_env_vars = [
  "ANTHROPIC_API_KEY",
  "OPENAI_API_KEY"
]
```

For `name_prefix = "harbor"`, the module creates `/harbor/extra-environment-variables/ANTHROPIC_API_KEY` and `/harbor/extra-environment-variables/OPENAI_API_KEY` with placeholder values. Replace the placeholders in SSM after apply. Terraform ignores later value drift.

Use `extra_env_var_parameter_names` when you already manage SSM SecureString parameters outside this module:

```hcl
extra_env_var_parameter_names = {
  API_KEY = "/harbor/runtime/API_KEY"
}
```

Terraform stores placeholder values for module-created parameters and stores only names for externally managed parameters. The instance profile receives `ssm:GetParameter` and `ssm:GetParameters` for those parameter ARNs when the module creates the profile. The generated `/etc/profile.d/extra-env-vars.sh` fetches values at shell startup and exports them into the shell environment.

## Tailscale

When `developer_config.enable_tailscale = true`, the module creates a SecureString placeholder at `/<name_prefix>/tailscale-auth-key`. Replace it with a real Tailscale auth key after apply. Do not open AWS security-group port `22` for Tailscale SSH.

## KMS Options

| Input | Applies to |
| --- | --- |
| `root_volume_kms_key_id` | EC2 root EBS volume |
| `workspace_log_group_kms_key_id` | EC2 CloudWatch log group |
| `ssm_parameter_kms_key_id` | Module-created SecureString placeholders |

## Inputs

| Name | Default | Description |
| --- | --- | --- |
| `aws_region` | required | Region used for bootstrap URLs and SSM parameter ARNs |
| `name_prefix` | required | Lowercase resource name prefix |
| `instance_name` | required | EC2 instance name |
| `vpc_id` | `null` | VPC ID, `default`, or null for default VPC discovery |
| `subnet_id` | `null` | Subnet ID, or null for first selected VPC subnet |
| `security_group_ids` | `[]` | Existing security groups, or generated group when empty |
| `associate_public_ip` | `false` | Explicit public IPv4 association |
| `ingress_ports` | `[]` | Generated ingress rules with explicit sources |
| `egress_ports` | `443/tcp`, `80/tcp`, `41641/udp` | Generated egress rules |
| `ami_id` | `null` | Optional pinned AMI ID |
| `root_volume_delete_on_termination` | `false` | Whether EC2 deletes the root volume on termination |
| `extra_env_vars` | `[]` | Environment variable names for module-created SSM SecureString placeholders |
| `extra_env_var_parameter_names` | `{}` | Environment variable names mapped to existing SSM parameter names |
| `enable_session_manager` | `true` | Enable Session Manager shell access |

See `variables.tf` for the complete schema.

## Outputs

| Name | Description |
| --- | --- |
| `instance_id` | EC2 instance ID |
| `instance_public_ip` | Public IP address when associated |
| `instance_private_ip` | Private IP address |
| `security_group_id` | Created security group ID, or null when existing groups are supplied |
| `vpc_id` | VPC ID used by the instance |
| `subnet_id` | Subnet ID used by the instance |
| `ami_id` | AMI ID used by the instance |
| `ami_boot_mode` | Resolved Ubuntu AMI boot mode, or null for custom AMIs |
| `root_volume_id` | Root EBS volume ID |
| `ssm_start_session_command` | Session Manager command, or null when disabled |

## Example

See `examples/basic` for a standalone EC2 submodule example.
