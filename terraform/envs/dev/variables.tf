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
  description = "Environment tag value."
  type        = string
  default     = "dev"
}

variable "name_prefix" {
  description = "Prefix for every resource name in this stack."
  type        = string
  default     = "fraud-lake"
}

# ------------------------------------------------------------------- cost levers

variable "enable_stream" {
  description = <<-EOT
    Create the Kinesis stream and Firehose delivery stream.
    COST: an on-demand stream bills ~$0.036/stream-hour whether or not it is written to
    — about $26-29/month if left running. Set false (`make stream-down`) between demo
    sessions; the lake, catalog, and Athena workgroup stay up and stay queryable.
  EOT
  type        = bool
  default     = true
}

variable "use_kms_cmk" {
  description = <<-EOT
    Encrypt the lake with a customer-managed KMS key instead of SSE-S3.
    COST: ~$1/month per key plus per-request charges. Default false.
  EOT
  type        = bool
  default     = false
}

variable "athena_scan_cutoff_bytes" {
  description = "Athena per-query scan cap. 1 GB."
  type        = number
  default     = 1073741824
}

variable "raw_retention_days" {
  description = "Days before raw/ objects expire."
  type        = number
  default     = 30
}

variable "athena_results_retention_days" {
  description = "Days before Athena results expire."
  type        = number
  default     = 7
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 7
}

variable "force_destroy_buckets" {
  description = "Let `terraform destroy` remove non-empty buckets. Dev only."
  type        = bool
  default     = true
}

# --------------------------------------------------------------- firehose tuning

variable "firehose_buffer_size_mb" {
  description = "Firehose buffer size in MB."
  type        = number
  default     = 5
}

variable "firehose_buffer_interval_seconds" {
  description = <<-EOT
    Firehose buffer interval in seconds. 300 is the default and the right production
    answer. Drop to 60 while demoing so data appears in S3 within a minute — the
    tradeoff is more, smaller files for Spark to open.
  EOT
  type        = number
  default     = 300
}
