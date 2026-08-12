variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "source_root" {
  description = "Absolute path to the repo root."
  type        = string
}

variable "lake_bucket_id" {
  type = string
}

variable "lake_bucket_arn" {
  type = string
}

variable "glue_job_names" {
  description = "Map of layer -> Glue job name, from the glue_jobs module."
  type        = map(string)
}

variable "bronze_table" {
  type    = string
  default = "fraud_bronze.transactions"
}

variable "silver_table" {
  type    = string
  default = "fraud_silver.transactions"
}

variable "alert_email" {
  description = "Email for pipeline alerts. Leave empty to skip the subscription. AWS sends a confirmation link you must click."
  type        = string
  default     = ""
}

variable "enable_schedule" {
  description = <<-EOT
    Enable the hourly EventBridge schedule.
    COST: each run costs Glue DPU-minutes even over zero new rows. Leave false and
    enable it only for the demo window.
  EOT
  type        = bool
  default     = false
}

variable "enable_s3_trigger" {
  description = "Start the pipeline when Firehose delivers to raw/transactions/."
  type        = bool
  default     = true
}

variable "freshness_hours" {
  description = "Alarm if no successful bronze write in this many hours."
  type        = number
  default     = 3
}

variable "knowledge_base_id" {
  description = "Bedrock Knowledge Base id. Empty until slice 2 creates it."
  type        = string
  default     = ""
}

variable "knowledge_base_data_source_id" {
  description = "Bedrock Knowledge Base data source id. Empty until slice 2 creates it."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 7
}
