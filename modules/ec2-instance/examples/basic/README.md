# Basic EC2 Submodule Example

This example is a standalone Terraform configuration for `modules/ec2-instance`. It owns the AWS provider configuration and calls the EC2 submodule directly.

## What It Does

- Uses the default VPC and first selected subnet unless you override inputs.
- Creates a module-managed security group because `security_group_ids = []` by default.
- Explicitly enables public IP association because the default VPC normally has no NAT Gateway and bootstrap needs outbound internet access.
- Keeps Session Manager enabled for shell access without inbound SSH.
- Disables Tailscale bootstrap so the example does not require a Tailscale auth key.

Use `associate_public_ip = false` with a private subnet that has NAT/proxy egress.

## Usage

```bash
terraform init
terraform plan
terraform apply
```

Print the generated Session Manager command:

```bash
terraform output -raw ssm_start_session_command
```
