provider "aws" {
  region = var.aws_region
}

module "harbor_workspace" {
  source = "../.."

  aws_region    = var.aws_region
  name_prefix   = var.name_prefix
  instance_name = var.instance_name
  # The default VPC has no NAT Gateway. This example opts into a public IP so
  # bootstrap can reach apt repositories, AWS downloads, and package sources.
  associate_public_ip = true

  developer_config = {
    enable_tailscale = false
  }

  scheduler_features = {
    reconcile = true
  }
}
