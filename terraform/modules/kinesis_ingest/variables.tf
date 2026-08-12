variable "name_prefix" {
  description = "Prefix for stream, delivery stream, and role names."
  type        = string
  default     = "fraud-lake"
}

variable "account_id" {
  description = "AWS account id, used as the Firehose sts:ExternalId condition."
  type        = string
}

variable "lake_bucket_arn" {
  description = "ARN of the lake bucket Firehose delivers into."
  type        = string
}

variable "kms_key_arn" {
  description = "CMK ARN if the lake uses one; null means SSE-S3 and the AWS-managed Kinesis key."
  type        = string
  default     = null
}

variable "buffer_size_mb" {
  description = "Firehose buffer size in MB (1-128)."
  type        = number
  default     = 5

  validation {
    condition     = var.buffer_size_mb >= 1 && var.buffer_size_mb <= 128
    error_message = "buffer_size_mb must be between 1 and 128."
  }
}

variable "buffer_interval_seconds" {
  description = "Firehose buffer interval in seconds (60-900)."
  type        = number
  default     = 300

  validation {
    condition     = var.buffer_interval_seconds >= 60 && var.buffer_interval_seconds <= 900
    error_message = "buffer_interval_seconds must be between 60 and 900."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Kept short — logs are a per-GB charge."
  type        = number
  default     = 7
}
