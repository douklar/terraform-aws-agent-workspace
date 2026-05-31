provider "aws" {
  region = var.aws_region
}

module "ec2_instance" {
  source = "../../"

  aws_region    = var.aws_region
  name_prefix   = "harbor"
  instance_name = "harbor-workspace"
  instance_type = "m7i-flex.large"
  # The default VPC has no NAT Gateway. This example opts into a public IP so
  # bootstrap can reach apt repositories, AWS downloads, and package sources.
  associate_public_ip = true

  developer_config = {
    enable_tailscale = false
  }
}
