variable "workgroup_name" {
  description = "Athena workgroup name."
  type        = string
  default     = "fraud-lake"
}

variable "lake_bucket_id" {
  description = "Lake bucket name."
  type        = string
}

variable "results_prefix" {
  description = "Prefix under the lake bucket for query results."
  type        = string
  default     = "athena-results"
}

variable "bytes_scanned_cutoff" {
  description = <<-EOT
    Per-query scan cap in bytes. 1 GB by default. Athena's minimum is 10 MB.
    This is the single most effective Athena cost control.
  EOT
  type        = number
  default     = 1073741824

  validation {
    condition     = var.bytes_scanned_cutoff >= 10485760
    error_message = "Athena requires a cutoff of at least 10485760 bytes (10 MB)."
  }
}

variable "kms_key_arn" {
  description = "CMK ARN for result encryption; null selects SSE-S3."
  type        = string
  default     = null
}

variable "databases" {
  description = "Glue Data Catalog databases to create, one per lake zone."
  type        = list(string)
  default     = ["fraud_raw", "fraud_bronze", "fraud_silver", "fraud_gold"]
}

variable "raw_database" {
  description = "Which of `databases` holds the raw external table."
  type        = string
  default     = "fraud_raw"
}

variable "create_raw_table" {
  description = "Create the partition-projected external table over raw/transactions/."
  type        = bool
  default     = true
}

variable "projection_start_date" {
  description = "Earliest date partition projection will consider (yyyy-MM-dd)."
  type        = string
  default     = "2026-01-01"
}

variable "force_destroy" {
  description = "Allow the workgroup to be destroyed with query history present."
  type        = bool
  default     = true
}
