# terraform-aws-agent-workspace

A Terraform module that deploys a personal AI agent workspace on AWS — an Ubuntu EC2 instance that automatically starts in the evening, stops at night, stays backed up, and is accessible via browser-based terminal without opening any ports.

> **Designed for:** developers who want a cloud workstation for running AI agents (Claude Code, Codex, etc.) without spending time on AWS infrastructure research.

---

## What it does

- Launches an Ubuntu 24.04 EC2 instance with Claude Code, VS Code Server, and Tailscale pre-installed
- Automatically starts and stops the instance on your schedule (saves ~70% on EC2 costs)
- Connects via AWS Session Manager — no SSH keys, no open ports, no VPN required
- Takes weekly AMI snapshots so you can roll back if something breaks
- Sends a budget alert if your AWS bill goes over a limit you set

---

## Quick start

```hcl
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.5"
    }
  }
}

provider "aws" {
  region = "eu-central-1"
}

module "workspace" {
  # Terraform Registry (recommended once published):
  source  = "YOUR_NAMESPACE/agent-workspace/aws"
  version = "~> 1.0"

  # Or directly from GitHub without publishing to the registry:
  # source = "github.com/YOUR_ORG/terraform-aws-agent-workspace?ref=v1.0.0"

  aws_region = "eu-central-1"
}
```

That's it. Everything else has a sensible default. Run `terraform init && terraform apply`.

After apply, connect to your instance:

```bash
aws ssm start-session --target <instance_id> --region eu-central-1
```

The instance ID is shown in the `instance_id` output.

---

## Set your timezone and schedule

The default schedule runs in `Europe/Rome`. Change it to match where you live:

```hcl
module "workspace" {
  source = "..."

  aws_region = "us-east-1"

  instance_schedule_windows = [
    {
      name       = "evenings"
      timezone   = "America/New_York"
      days       = ["MON", "TUE", "WED", "THU", "FRI"]
      start_time = "17:00"
      stop_time  = "23:00"
    },
    {
      name       = "weekends"
      timezone   = "America/New_York"
      days       = ["SAT", "SUN"]
      start_time = "10:00"
      stop_time  = "22:00"
    }
  ]
}
```

Timezone names follow the [IANA tz database](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) — for example `Europe/Berlin`, `America/Los_Angeles`, `Asia/Tokyo`.

---

## Common configurations

### Need more disk or CPU?

```hcl
module "workspace" {
  source = "..."

  instance_type = "m7i-flex.xlarge"   # 4 vCPU, 16 GB RAM
  storage       = { size_gb = 100 }   # 100 GB disk (default: 30 GB)
}
```

Supported instance families: `t3`, `t3a`, `m5`, `m6i`, `m7i`, `m7i-flex`, `c5`, `c6i`, `c7i`, `r5`, `r6i`, `r7i`.

### Store API keys securely

The module creates encrypted parameter placeholders in AWS. You fill in the real values after deploy — Terraform never sees or stores the secrets.

```hcl
module "workspace" {
  source = "..."

  extra_env_vars = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"]
}
```

After `terraform apply`, set each key:

```bash
aws ssm put-parameter \
  --name "/workspace/extra-environment-variables/ANTHROPIC_API_KEY" \
  --type SecureString \
  --value "sk-ant-..." \
  --overwrite \
  --region eu-central-1
```

The instance loads these automatically at shell startup. Change `workspace` to your `name_prefix` if you set one.

### Connect with Tailscale

Tailscale is installed by default. After `terraform apply`, set your auth key:

```bash
aws ssm put-parameter \
  --name "/workspace/tailscale-auth-key" \
  --type SecureString \
  --value "tskey-auth-..." \
  --overwrite \
  --region eu-central-1
```

Then reboot or restart the Tailscale service on the instance. The instance will appear in your Tailscale network — no open ports needed.

To disable Tailscale:

```hcl
developer_config = { enable_tailscale = false }
```

### Enable automated backups

```hcl
scheduler_features = {
  weekly_amis    = true   # AMI every Sunday night
  backup_cleanup = true   # auto-delete old AMIs
  security_update = true  # apply security patches weekly
}
```

By default, weekly AMIs are kept for 7 days and monthly AMIs for 365 days. To keep them longer:

```hcl
scheduler_features = {
  weekly_amis                  = true
  backup_cleanup               = true
  daily_snapshot_retention_days = 14
  monthly_ami_retention_days    = 730
}
```

### Set a budget alert

```hcl
cost_report = {
  enabled                  = true
  email_addresses          = ["you@example.com"]
  monthly_budget_limit_usd = 50
  alert_threshold_percent  = 80   # alert at $40
}
```

### Use your own KMS key for encryption

Everything is encrypted with AWS-managed keys by default. To use your own KMS key for all resources:

```hcl
kms_key_arn = "arn:aws:kms:eu-central-1:123456789012:key/mrk-abc123"
```

This applies to: EBS volume, SSM parameters, SQS queues, S3 export bucket, and CloudWatch logs.

---

## Networking

The instance is **private by default** — no public IP, no open inbound ports.

Access works through two channels that don't need open ports:
- **AWS Session Manager** — browser or CLI terminal, enabled by default
- **Tailscale** — mesh VPN, optional

If you're using the default VPC without a NAT Gateway, set:

```hcl
associate_public_ip = true
```

For existing VPC/subnet infrastructure:

```hcl
vpc_id    = "vpc-0abc123"
subnet_id = "subnet-0abc123"
```

---

## All options at a glance

| Variable | Default | What it does |
|---|---|---|
| `aws_region` | `eu-central-1` | Where to deploy |
| `name_prefix` | `workspace` | Prefix for all resource names |
| `instance_name` | `workspace-ec2` | Name tag on the EC2 instance |
| `instance_type` | `m7i-flex.large` | CPU / RAM size |
| `storage` | `{ size_gb=30, encrypted=true }` | Disk size and encryption |
| `associate_public_ip` | `false` | Attach a public IP |
| `vpc_id` | `null` (default VPC) | VPC to deploy into |
| `subnet_id` | `null` (first subnet) | Specific subnet |
| `ami_id` | `null` (latest Ubuntu 24.04) | Pin a specific AMI |
| `developer_config` | All tools enabled | What to install at boot |
| `instance_schedule_windows` | Evenings + weekends, Rome | When the instance runs |
| `scheduler_features` | Reconcile only | Backups, patches, cleanup |
| `extra_env_vars` | `[]` | API keys to inject as env vars |
| `kms_key_arn` | `null` (AWS-managed keys) | Your own KMS key |
| `cost_report` | Disabled | AWS Budgets email alert |
| `ami_transfer` | Disabled | Copy or export AMIs |
| `enable_session_manager` | `true` | Browser/CLI terminal access |
| `tags` | `{}` | Extra tags on all resources |

---

## Encryption defaults (no KMS key needed)

| Resource | Encrypted without KMS? |
|---|---|
| EBS root volume | Yes — AWS-managed EBS key |
| SSM parameters | Yes — AWS-managed SSM key |
| SQS queues | Yes — SQS-managed SSE (free) |
| S3 export bucket | Yes — SSE-S3 (AES-256) |
| CloudWatch logs | No — AWS requires a KMS key for log group encryption |

---

## After apply checklist

1. **Set your Tailscale auth key** (if `developer_config.enable_tailscale = true`)
2. **Set your API keys** (if you used `extra_env_vars`)
3. **Connect**: `aws ssm start-session --target <instance_id>`
4. The instance starts automatically at your scheduled time

---

## Requirements

| Tool | Version |
|---|---|
| Terraform | `>= 1.9.0, < 2.0.0` |
| AWS provider | `~> 5.0` |
| Archive provider | `~> 2.5` |
| AWS CLI | Any recent version (for SSM sessions) |

AWS credentials must have permission to create EC2, IAM, Lambda, EventBridge, SQS, SSM, and CloudWatch resources.

---

## Manual AMI export (advanced)

If `ami_transfer.enable_export = true`, AWS requires a pre-existing IAM role named `vmimport` (configurable via `ami_transfer.export_role_name`). This role must trust `vmie.amazonaws.com`. See the [AWS VM Import/Export documentation](https://docs.aws.amazon.com/vm-import/latest/userguide/required-permissions.html) for the exact trust policy and permissions.

---

## Submodule

The EC2 instance layer is also available as a standalone submodule in `modules/ec2-instance` — useful if you only need the instance without the scheduler, backups, and AMI transfer infrastructure.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) for how to report vulnerabilities.

## License

Apache 2.0 — see [LICENSE](LICENSE).
