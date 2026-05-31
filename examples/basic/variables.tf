variable "aws_region" {
  description = "AWS region for the example deployment"
  type        = string
  default     = "eu-central-1"
}

variable "name_prefix" {
  description = "Name prefix for example resources"
  type        = string
  default     = "harbor"
}

variable "instance_name" {
  description = "Name tag for the example workspace instance"
  type        = string
  default     = "harbor-workspace"
}
