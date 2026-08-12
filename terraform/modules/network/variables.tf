variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

variable "region" {
  type = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "az_count" {
  description = "Availability zones. 2 is the minimum an ALB accepts."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "An ALB requires at least 2 AZs; more than 3 multiplies endpoint cost for no benefit here."
  }
}

variable "networking_mode" {
  description = <<-EOT
    "public_tasks" (default, $0/month): Fargate tasks in public subnets with a public IP
    and no inbound access except from the ALB. Egress via the internet gateway, which is
    free.

    "endpoints" (~$130/month while up): tasks in private subnets with no internet route,
    reaching AWS services through 10 interface endpoints across 2 AZs at ~$7.30/month
    each. Production-shaped, and more expensive than the NAT gateway it replaces at this
    scale. Tear down the same day.

    Neither mode creates a NAT gateway.
  EOT
  type        = string
  default     = "public_tasks"

  validation {
    condition     = contains(["public_tasks", "endpoints"], var.networking_mode)
    error_message = "networking_mode must be \"public_tasks\" or \"endpoints\"."
  }
}
