<p align="center">
  <img src="https://raw.githubusercontent.com/douklar/douklar/main/assets/logo.png" alt="Douklar DevOps Tools" width="260" />
</p>

<h1 align="center">terraform-aws-agent-workspace</h1>

<p align="center">
  <a href="https://registry.terraform.io/modules/douklar/agent-workspace/aws"><img src="https://img.shields.io/badge/Terraform%20Registry-douklar%2Fagent--workspace-7B42BC?style=flat-square&logo=terraform&logoColor=white" alt="Terraform Registry" /></a>
  <img src="https://img.shields.io/badge/Terraform-%3E%3D1.9-7B42BC?style=flat-square&logo=terraform&logoColor=white" alt="Terraform >= 1.9" />
  <img src="https://img.shields.io/badge/AWS%20Provider-~%3E%206.47-FF9900?style=flat-square&logo=amazonaws&logoColor=white" alt="AWS Provider ~> 6.47" />
  <img src="https://img.shields.io/badge/Ubuntu-24.04%20LTS-E95420?style=flat-square&logo=ubuntu&logoColor=white" alt="Ubuntu 24.04 LTS" />
  <img src="https://img.shields.io/badge/License-Apache%202.0-blue?style=flat-square" alt="Apache 2.0" />
</p>

<p align="center">
  A Terraform module that provisions a personal AI agent workspace on AWS — an Ubuntu 24.04 EC2 instance with Claude Code, VS Code Server, and Tailscale pre-installed, accessed securely through AWS Session Manager with no open ports.
</p>

<p align="center">
  <em>Part of the <strong>Douklar DevOps Tools</strong> series.</em>
</p>

---

## Overview

This module is designed for developers who want a ready-to-use cloud workstation for running AI agents (Claude Code, Codex, etc.) without investing time in AWS infrastructure setup.

| Capability | Detail |
|:---|:---|
| **Zero-exposure access** | AWS Session Manager and Tailscale — no SSH keys, no inbound ports required |
| **Cost-optimized scheduling** | Automatic start/stop on your schedule, reducing EC2 costs by ~70% |
| **Pre-installed tooling** | Claude Code, VS Code Server, Tailscale; optional OpenAI Codex CLI |
| **Secrets management** | API keys stored in AWS SSM Parameter Store — Terraform never sees the values |
| **Automated backups** | Daily EBS snapshots, weekly and monthly AMIs with configurable retention |
| **Encryption by default** | EBS, SSM parameters, SQS, and S3 are all encrypted without a custom KMS key |
| **Budget protection** | Native AWS Budgets alert at a cost threshold you define |

---

## Usage

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  aws_region = "eu-central-1"
}
```

Run `terraform init && terraform apply`. All variables have sensible defaults.

After apply, connect to your instance using the ready-to-run output:

```bash
terraform output -raw ssm_start_session_command
```

Or connect directly:

```bash
aws ssm start-session --target $(terraform output -raw instance_id) --region eu-central-1
```

See [`examples/basic`](examples/basic) for a complete working configuration, or copy [`terraform.tfvars.example`](terraform.tfvars.example) as a starting point.

---

## Configuration

### Schedule

The instance starts and stops automatically according to configurable time windows. The default schedule runs in `Europe/Berlin` on weekday evenings and weekends.

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  aws_region = "eu-central-1"

  instance_schedule_windows = [
    {
      name       = "evenings"
      timezone   = "Europe/Berlin"
      days       = ["MON", "TUE", "WED", "THU", "FRI"]
      start_time = "18:30"
      stop_time  = "23:00"
    },
    {
      name       = "weekends"
      timezone   = "Europe/Berlin"
      days       = ["SAT", "SUN"]
      start_time = "11:00"
      stop_time  = "22:00"
    }
  ]
}
```

Timezone strings follow the [IANA tz database](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) — for example `Europe/Berlin`, `Europe/London`, `Asia/Tokyo`.

### Instance size

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  instance_type = "m7i-flex.xlarge"  # 4 vCPU, 16 GB RAM
  storage       = { size_gb = 100 }  # default: 30 GB
}
```

Supported families: `t3`, `t3a`, `m5`, `m6i`, `m7i`, `m7i-flex`, `c5`, `c6i`, `c7i`, `r5`, `r6i`, `r7i`.

### API keys and secrets

The module creates encrypted SSM Parameter Store placeholders. Terraform never reads or stores the secret values — you populate them after apply.

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  extra_env_vars = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"]
}
```

After `terraform apply`, write each value:

```bash
aws ssm put-parameter \
  --name "/workspace/extra-environment-variables/ANTHROPIC_API_KEY" \
  --type SecureString \
  --value "sk-ant-..." \
  --overwrite \
  --region eu-central-1
```

The instance loads all injected variables automatically at shell startup. Replace `workspace` with your `name_prefix` if you customized it.

### Developer tooling

All tools except Codex CLI are enabled by default. Override to control what gets installed:

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  developer_config = {
    install_vscode      = true   # VS Code Server
    enable_tailscale    = true   # Tailscale mesh VPN
    install_claude_code = true   # Claude Code CLI
    install_codex_cli   = false  # OpenAI Codex CLI (disabled by default)
  }
}
```

### Tailscale

Tailscale is installed and enabled by default. After `terraform apply`, write your auth key:

```bash
aws ssm put-parameter \
  --name "/workspace/tailscale-auth-key" \
  --type SecureString \
  --value "tskey-auth-..." \
  --overwrite \
  --region eu-central-1
```

Reboot or restart the Tailscale service on the instance to activate. The instance joins your Tailscale network with no inbound ports open.

#### Tailscale access control policy

Apply this ACL in your [Tailscale admin console](https://login.tailscale.com/admin/acls) to restrict access to the workspace. Only Tailscale admins can reach it on port 22 or open a Tailscale SSH session — no other users or devices can connect. Advanced users can customize the policy further — for example adding more tags, user groups, or port rules — directly in the Tailscale console.

```json
{
    "tagOwners": {
        "tag:ssh-enabled": ["autogroup:admin"]
    },

    "acls": [
        {
            "action": "accept",
            "src":    ["autogroup:admin"],
            "dst":    ["tag:ssh-enabled:22"]
        }
    ],

    "ssh": [
        {
            "action": "accept",
            "src":    ["autogroup:admin"],
            "dst":    ["tag:ssh-enabled"],
            "users":  ["ubuntu"]
        }
    ]
}
```

To apply this tag to the workspace device, generate a **pre-authenticated, tagged auth key** in the Tailscale admin console (`Settings → Keys → Generate auth key`) and enable the `tag:ssh-enabled` tag on it. Then write that key to SSM instead of a plain auth key — the device will inherit the tag automatically on join.

### Automated backups

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  scheduler_features = {
    daily_snapshots               = true            # nightly EBS snapshot
    weekly_amis                   = true            # full AMI every Sunday
    monthly_amis                  = true            # full AMI on the 1st
    backup_cleanup                = true            # auto-expire old backups
    security_update               = true            # weekly security patches
    daily_snapshot_retention_days = 7
    monthly_ami_retention_days    = 30
    maintenance_timezone          = "Europe/Berlin"
  }
}
```

**`scheduler_features` options:**

| Option | Default | Description |
|:---|:---:|:---|
| `reconcile` | `true` | Keep instance state in sync with the schedule |
| `daily_snapshots` | `false` | Take a nightly EBS snapshot |
| `weekly_amis` | `false` | Create a full AMI every Sunday night |
| `monthly_amis` | `false` | Create a full AMI on the 1st of each month |
| `backup_cleanup` | `false` | Auto-delete backups past the retention period |
| `security_update` | `false` | Apply security patches on a weekly schedule |
| `maintenance_timezone` | `Europe/Berlin` | Timezone for all maintenance jobs |
| `daily_snapshot_retention_days` | `7` | Days to retain daily EBS snapshots |
| `monthly_ami_retention_days` | `30` | Days to retain monthly AMIs |

### Budget alert

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  cost_report = {
    enabled                  = true
    email_addresses          = ["you@example.com"]
    monthly_budget_limit_usd = 50
    alert_threshold_percent  = 80  # sends alert at $40
  }
}
```

### Customer-managed KMS key

All resources use AWS-managed keys by default. To use your own KMS key across all resources:

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  kms_key_arn = "arn:aws:kms:eu-central-1:123456789012:key/mrk-abc123"
}
```

This applies to: EBS volume, SSM parameters, SQS queues, S3 export bucket, and CloudWatch Logs.

---

## Networking

The instance is **private by default** — no public IP, no open inbound ports.

| Access method | Description | Requirement |
|:---|:---|:---|
| AWS Session Manager | Browser or CLI terminal via IAM-authenticated SSM | IAM permissions |
| Tailscale | Encrypted mesh VPN, peer-to-peer | Auth key written to SSM |

If you are using the default VPC without a NAT Gateway, the instance needs a public IP to reach AWS endpoints:

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  associate_public_ip = true
}
```

For an existing VPC and subnet:

```hcl
module "workspace" {
  source  = "douklar/agent-workspace/aws"
  version = "~> 1.0"

  vpc_id    = "vpc-0abc123"
  subnet_id = "subnet-0abc123"
}
```

---

## Inputs

Full descriptions and validation rules are in [variables.tf](variables.tf).

| Name | Default | Description |
|:---|:---|:---|
| `aws_region` | `eu-central-1` | AWS region to deploy into |
| `name_prefix` | `workspace` | Prefix applied to all resource names |
| `instance_name` | `workspace-ec2` | Name tag on the EC2 instance |
| `instance_type` | `m7i-flex.large` | EC2 instance type |
| `storage` | `{ size_gb=30, encrypted=true }` | Root EBS volume size and encryption |
| `associate_public_ip` | `false` | Attach a public IPv4 address |
| `vpc_id` | `null` | VPC to deploy into (`null` = default VPC) |
| `subnet_id` | `null` | Subnet to use (`null` = first available) |
| `security_group_ids` | `[]` | Security group override (module creates one if empty) |
| `ingress_ports` | `[]` | Additional inbound security group rules |
| `egress_ports` | `443, 80, 41641/udp` | Outbound security group rules |
| `ami_id` | `null` | AMI override (`null` = latest Ubuntu 24.04 LTS) |
| `developer_config` | Claude Code + VS Code + Tailscale enabled; Codex CLI disabled | Tooling installed at boot |
| `instance_schedule_windows` | Evenings + weekends, Berlin | Start/stop schedule windows |
| `scheduler_features` | reconcile only | Backup, patch, and cleanup jobs |
| `extra_env_vars` | `[]` | Create SSM placeholders for environment variables |
| `extra_env_var_parameter_names` | `{}` | Map env var names to existing SSM parameters |
| `kms_key_arn` | `null` | Customer-managed KMS key ARN |
| `cost_report` | disabled | AWS Budgets cost alert |
| `ami_transfer` | disabled | AMI copy or export configuration |
| `enable_session_manager` | `true` | Enable AWS Session Manager access |
| `environment` | `dev` | Environment tag (`dev`, `staging`, `prod`) |
| `cost_center` | `engineering` | Cost center tag for billing attribution |
| `owner_email` | `owner@example.com` | Owner tag for operational responsibility |
| `tags` | `{}` | Additional tags applied to all resources |

---

## Outputs

| Name | Description |
|:---|:---|
| `instance_id` | EC2 instance ID |
| `instance_public_ip` | Public IP address (if `associate_public_ip = true`) |
| `ami_id` | AMI used to launch the instance |
| `root_volume_id` | Root EBS volume ID |
| `ssm_start_session_command` | Ready-to-run `aws ssm start-session` command |
| `workspace_log_group_name` | CloudWatch log group for bootstrap and instance logs |
| `scheduler_lambda_name` | Name of the EventBridge scheduler Lambda |
| `scheduler_lambda_arn` | ARN of the EventBridge scheduler Lambda |
| `scheduler_names` | All EventBridge Scheduler schedule names created |
| `ami_transfer_lambda_name` | AMI transfer Lambda name (null if transfer disabled) |
| `manual_export_latest_ami_example` | Example `aws lambda invoke` command to export the latest AMI (null if export disabled) |
| `ami_export_bucket_name` | S3 bucket used for AMI exports (null if export disabled) |
| `scheduler_dlq_arn` | ARN of the scheduler Lambda dead-letter queue |
| `cost_report_enabled` | Whether the AWS Budgets alert is active |

---

## Encryption reference

| Resource | Default encryption | Supports KMS override |
|:---|:---|:---:|
| EBS root volume | AWS-managed EBS key | Yes |
| SSM parameters | AWS-managed SSM key | Yes |
| SQS queues | SSE-SQS (no additional cost) | Yes |
| S3 export bucket | SSE-S3 (AES-256) | Yes |
| CloudWatch Logs | None by default | Yes (required for log encryption) |

---

## After apply checklist

1. If `developer_config.enable_tailscale = true` — write the Tailscale auth key to SSM
2. If `extra_env_vars` is configured — write each secret value to SSM
3. Connect: run `terraform output -raw ssm_start_session_command` and execute the result
4. The instance starts automatically at the next scheduled window

---

## Requirements

| Requirement | Version |
|:---|:---|
| Terraform | `>= 1.9.0, < 2.0.0` |
| AWS Provider | `~> 6.47` |
| Archive Provider | `~> 2.5` |
| AWS CLI | Any recent version (for `ssm start-session`) |

AWS credentials must have permission to create and manage: EC2, IAM, Lambda, EventBridge, SQS, SSM Parameter Store, S3, and CloudWatch.

---

## Advanced: AMI export

When `ami_transfer.enable_export = true`, AWS VM Import/Export requires a pre-existing IAM role named `vmimport` (configurable via `ami_transfer.export_role_name`). The role must trust the `vmie.amazonaws.com` service principal.

See [AWS VM Import/Export — Required permissions](https://docs.aws.amazon.com/vm-import/latest/userguide/required-permissions.html) for the exact trust policy and S3 bucket policy.

After apply, the `manual_export_latest_ami_example` output prints the exact `aws lambda invoke` command to trigger an export — no manual construction needed:

```bash
terraform output -raw manual_export_latest_ami_example
```

---

## Submodule

`modules/ec2-instance` is available as a standalone module for cases where you need only the EC2 instance without the scheduler, backup, and AMI transfer infrastructure.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
