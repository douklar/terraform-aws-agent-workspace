# Basic Root Module Example

This is a standalone Terraform root configuration for the complete Harbor module. It owns the AWS provider configuration and calls the repository root as a child module.

## Usage

```bash
terraform init
terraform plan
terraform apply
```

The example uses the default VPC, which normally has no NAT Gateway. It explicitly sets `associate_public_ip = true` so bootstrap can reach apt repositories, AWS downloads, and package sources. The generated security group still has no inbound rules by default.

Use `associate_public_ip = false` with a private subnet that has NAT/proxy egress.
