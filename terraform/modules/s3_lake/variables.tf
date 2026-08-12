variable "name_prefix" {
  description = "Bucket name prefix; the account id is appended to make it globally unique."
  type        = string
  default     = "fraud-lake"
}

variable "account_id" {
  description = "AWS account id, supplied by the caller from aws_caller_identity."
  type        = string
}

variable "use_kms_cmk" {
  description = <<-EOT
    Encrypt with a customer-managed KMS key instead of SSE-S3.
    COST: a CMK carries a ~$1/month floor plus per-request charges. Default false.
  EOT
  type        = bool
  default     = false
}

variable "force_destroy" {
  description = "Allow `terraform destroy` to delete a non-empty bucket. Dev only."
  type        = bool
  default     = true
}

variable "raw_retention_days" {
  description = "Days before objects under raw/ expire."
  type        = number
  default     = 30
}

variable "athena_results_retention_days" {
  description = "Days before Athena query results expire."
  type        = number
  default     = 7
}
