variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

variable "region" {
  type = string
}

variable "repositories" {
  description = "ECR repositories to create."
  type        = list(string)
  default     = ["api", "mcp"]
}

variable "force_delete_repositories" {
  description = "Let destroy remove repositories that still contain images. Dev only."
  type        = bool
  default     = true
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "task_subnet_ids" {
  type = list(string)
}

variable "assign_public_ip" {
  description = "True in public_tasks networking mode — the task's route to AWS APIs."
  type        = bool
  default     = true
}

variable "task_role_arn" {
  description = "The agent role from the bedrock module. Separate from the execution role."
  type        = string
}

variable "api_image" {
  description = "Full ECR image URI. Push an image before setting desired_count above 0."
  type        = string
}

variable "container_port" {
  type    = number
  default = 8000
}

variable "environment" {
  description = "Environment variables for the container."
  type        = map(string)
  default     = {}
}

# ------------------------------------------------------------------- cost controls

variable "desired_count" {
  description = <<-EOT
    Running task count. DEFAULTS TO 0 — Fargate bills per second while a task runs, and
    the steady state of this project is nothing running. Set to 1 for a demo, then back
    to 0. Note that this does NOT stop ALB charges; see enable_alb.
  EOT
  type        = number
  default     = 0
}

variable "enable_alb" {
  description = <<-EOT
    Create the Application Load Balancer.
    COST: ~$16/month plus LCU charges, billed from creation, INDEPENDENT of desired_count.
    This is the trap — setting desired_count to 0 and leaving the ALB up looks free and
    is not. Leave false unless a demo needs a stable public URL.
  EOT
  type        = bool
  default     = false
}

variable "allowed_ingress_cidrs" {
  description = "CIDRs allowed to reach the ALB. Set to your own IP; never 0.0.0.0/0."
  type        = list(string)
  default     = ["127.0.0.1/32"]
}

variable "task_cpu" {
  description = "Fargate CPU units. 256 = 0.25 vCPU, the smallest available."
  type        = number
  default     = 256

  validation {
    condition     = var.task_cpu <= 512
    error_message = "The workload is IO-bound; more than 0.5 vCPU buys nothing but a higher rate."
  }
}

variable "task_memory" {
  description = "Fargate memory in MB. 512 is the minimum for 256 CPU units."
  type        = number
  default     = 512
}

variable "log_retention_days" {
  type    = number
  default = 7
}
