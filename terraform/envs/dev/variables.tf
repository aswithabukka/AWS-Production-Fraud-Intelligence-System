variable "region" {
  description = "AWS region. One region for the whole project."
  type        = string
  default     = "us-east-1"
}

variable "profile" {
  description = "AWS CLI profile. Never the root account."
  type        = string
  default     = "fraud-lake"
}

variable "env" {
  type    = string
  default = "dev"
}

variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

# ==================================================================== COST LEVERS
# Every variable in this block controls something that costs money while it exists.
# The defaults are the cheap ones.

variable "enable_stream" {
  description = <<-EOT
    Create the Kinesis stream and Firehose delivery stream.
    COST: an on-demand stream bills ~$0.036/stream-hour whether or not it is written to
    — about $26-29/month if left running. `make stream-down` between demo sessions; the
    lake, catalog, and Athena workgroup stay up and queryable.
  EOT
  type        = bool
  default     = true
}

variable "enable_schedule" {
  description = <<-EOT
    Arm the hourly EventBridge schedule.
    COST: each run costs Glue DPU-minutes even over zero new rows. Enable for a demo
    window, then disable.
  EOT
  type        = bool
  default     = false
}

variable "enable_s3_trigger" {
  description = "Start the pipeline when Firehose delivers to raw/. Per-invocation, effectively free."
  type        = bool
  default     = true
}

variable "enable_agent_layer" {
  description = <<-EOT
    Create the Bedrock Knowledge Base (S3 Vectors), Guardrails, and the agent IAM role.
    COST: per-request only — no idle charge. Requires Bedrock model access to be enabled
    in the console first, and AWS provider >= 6.0 for the S3 Vectors resources.
  EOT
  type        = bool
  default     = false
}

variable "enable_containers" {
  description = <<-EOT
    Create the VPC, ECR repositories, and the ECS service.
    Costs nothing at rest with ecs_desired_count = 0 and enable_alb = false.
  EOT
  type        = bool
  default     = false
}

variable "ecs_desired_count" {
  description = "Running Fargate tasks. 0 is the steady state; Fargate bills per second."
  type        = number
  default     = 0
}

variable "enable_alb" {
  description = <<-EOT
    Create the Application Load Balancer.
    COST: ~$16/month, billed from creation and INDEPENDENT of ecs_desired_count. This is
    the trap: 0 tasks plus a live ALB looks free and is not.
  EOT
  type        = bool
  default     = false
}

variable "networking_mode" {
  description = <<-EOT
    "public_tasks" ($0/month): tasks in public subnets, egress via the internet gateway,
    no inbound except from the ALB.
    "endpoints" (~$130/month while up): tasks in private subnets behind 10 interface
    endpoints — more expensive than the NAT gateway it replaces at this scale.
    Neither mode creates a NAT gateway.
  EOT
  type        = string
  default     = "public_tasks"
}

variable "enable_lake_formation" {
  description = "Register the lake and create the analyst / risk_analyst personas. Free."
  type        = bool
  default     = false
}

variable "enable_cloudtrail" {
  description = <<-EOT
    Create the audit trail. The first management-event trail is free; S3 data events are
    per-event and are scoped to the lake bucket only.
  EOT
  type        = bool
  default     = false
}

variable "use_kms_cmk" {
  description = "Customer-managed KMS key instead of SSE-S3. COST: ~$1/month per key."
  type        = bool
  default     = false
}

# ============================================================== tuning and limits

variable "athena_scan_cutoff_bytes" {
  description = "Athena per-query scan cap. 1 GB."
  type        = number
  default     = 1073741824
}

variable "glue_worker_type" {
  type    = string
  default = "G.1X"
}

variable "glue_number_of_workers" {
  type    = number
  default = 2
}

variable "glue_timeout_minutes" {
  type    = number
  default = 15
}

variable "max_iterations" {
  description = "Hard stop on the agent's tool-calling loop."
  type        = number
  default     = 5
}

variable "freshness_hours" {
  description = "Alarm if no successful bronze write in this many hours."
  type        = number
  default     = 3
}

variable "raw_retention_days" {
  type    = number
  default = 30
}

variable "athena_results_retention_days" {
  type    = number
  default = 7
}

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "force_destroy_buckets" {
  description = "Let destroy remove non-empty buckets. Dev only."
  type        = bool
  default     = true
}

variable "firehose_buffer_size_mb" {
  type    = number
  default = 5
}

variable "firehose_buffer_interval_seconds" {
  description = "300 is the production answer; 60 makes data appear within a minute while demoing."
  type        = number
  default     = 300
}

# ======================================================================== models
# Named explicitly so the agent's IAM policy can list exact ARNs. A wildcard would let a
# config change silently switch to a model costing far more per token, with no
# infrastructure change to review.

variable "routing_model_id" {
  description = "Fast, cheap model for routing decisions."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "sql_model_id" {
  description = "SQL generation — structured, low-creativity, so the cheap tier fits."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "synthesis_model_id" {
  description = "Final synthesis only. The one place the larger model earns its cost."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

# ======================================================================== misc

variable "alert_email" {
  description = "Pipeline alerts. AWS sends a confirmation link you must click."
  type        = string
  default     = ""
}

variable "api_image" {
  description = "ECR image URI for the API. Push an image before raising ecs_desired_count."
  type        = string
  default     = "public.ecr.aws/docker/library/python:3.12-slim"
}

variable "allowed_ingress_cidrs" {
  description = "CIDRs allowed to reach the ALB. Set to your own IP; never 0.0.0.0/0."
  type        = list(string)
  default     = ["127.0.0.1/32"]
}

variable "fallback_task_role_arn" {
  description = "Task role used when the agent layer is disabled. Rarely needed."
  type        = string
  default     = ""
}
