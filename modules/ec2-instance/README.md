# Harbor EC2 Instance Module

This submodule provisions one Ubuntu EC2 developer workspace with CloudWatch logging, optional Session Manager access, optional Tailscale bootstrap, and optional SSM-backed runtime environment variables. Most users should consume the repository root module instead.

## Usage

Configure providers in your root Terraform configuration:

```hcl
terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.47.0"
    }
  }
}

provider "aws" {
  region = "eu-central-1"
}

module "workspace_ec2" {
  source = "git::https://github.com/douklar/terraform-aws-agent-workspace.git//modules/ec2-instance?ref=v1.0.0"

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

Use `env_vars` to inject secrets into the instance. Each key is the environment variable name. Set the value to `null` to have the module create an SSM SecureString placeholder (populate the value in SSM after apply), or set it to an existing SSM parameter path to reference it directly without creating a new parameter:

```hcl
env_vars = {
  ANTHROPIC_API_KEY = null                    # module creates placeholder — populate after apply
  OPENAI_API_KEY    = null                    # module creates placeholder — populate after apply
  API_KEY           = "/harbor/runtime/API_KEY" # reference an existing SSM parameter
}
```

For `name_prefix = "harbor"`, the module creates `/harbor/extra-environment-variables/ANTHROPIC_API_KEY` and `/harbor/extra-environment-variables/OPENAI_API_KEY` with placeholder values. Replace the placeholders in SSM after apply. Terraform ignores later value drift.

The instance profile receives `ssm:GetParameter` and `ssm:GetParameters` for all referenced parameter ARNs. The generated `/etc/profile.d/extra-env-vars.sh` fetches values at shell startup and exports them into the shell environment.

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
| `instance_type` | `m7i-flex.large` | UEFI-capable x86_64 Nitro EC2 instance type |
| `vpc_id` | `null` | VPC ID, `"default"`, or `null` for default VPC discovery |
| `subnet_id` | `null` | Subnet ID, or `null` for first available VPC subnet |
| `security_group_ids` | `[]` | Existing security groups; module creates one when empty |
| `associate_public_ip` | `false` | Explicit public IPv4 association |
| `ingress_ports` | `[]` | Generated ingress rules with explicit sources |
| `egress_ports` | `443/tcp, 80/tcp, 41641/udp` | Generated egress rules |
| `ami_id` | `null` | Optional pinned AMI ID (`null` = latest Ubuntu 24.04 LTS) |
| `root_volume_size` | `30` | Root EBS volume size in GB |
| `root_volume_type` | `gp3` | Root EBS volume type (`gp2`, `gp3`, `io1`, `io2`, `standard`) |
| `root_volume_encrypted` | `true` | Encrypt the root EBS volume |
| `root_volume_kms_key_id` | `null` | KMS key ID or ARN for root EBS volume encryption |
| `root_volume_delete_on_termination` | `false` | Whether EC2 deletes the root volume on termination |
| `iam_instance_profile_name` | `null` | Existing IAM instance profile name; module creates one when `null` |
| `developer_config` | Claude Code + VS Code + Tailscale on; Codex CLI off | Tooling installed at bootstrap |
| `env_vars` | `{}` | Map env var names to SSM parameter paths; `null` value creates a placeholder |
| `enable_session_manager` | `true` | Enable Session Manager shell access |
| `scheduler_mode` | `free-time` | Initial value of the `scheduler` tag; set at creation only (runtime overrides are preserved) |
| `workspace_log_group_kms_key_id` | `null` | KMS key ID or ARN for the CloudWatch log group |
| `ssm_parameter_kms_key_id` | `null` | KMS key ID or ARN for module-created SecureString parameters |
| `tags` | `{}` | Additional tags applied to all resources |

See `variables.tf` for the complete schema and validation rules.

## Outputs

| Name | Description |
| --- | --- |
| `instance_id` | EC2 instance ID |
| `instance_public_ip` | Public IP address when associated |
| `instance_private_ip` | Private IP address |
| `security_group_id` | Created security group ID, or `null` when existing groups are supplied |
| `vpc_id` | VPC ID used by the instance |
| `subnet_id` | Subnet ID used by the instance |
| `ami_id` | AMI ID used by the instance |
| `ami_boot_mode` | Resolved Ubuntu AMI boot mode, or `null` for custom AMIs |
| `root_volume_id` | Root EBS volume ID |
| `ssm_start_session_command` | Session Manager command, or `null` when disabled |
| `iam_instance_profile_name` | IAM instance profile name, or `null` when an existing profile is supplied |
| `cloudwatch_log_group_name` | CloudWatch log group for bootstrap, cloud-init, and instance logs |
| `ssm_parameter_tailscale_arn` | ARN of the Tailscale auth key SSM parameter (sensitive) |
| `ssm_parameter_instance_name_arn` | ARN of the instance name SSM parameter |

## Example

See `examples/basic` for a standalone EC2 submodule example.
