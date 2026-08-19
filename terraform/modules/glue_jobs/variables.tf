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
  description = "Absolute path to the repo root, used to upload the job scripts."
  type        = string
}

variable "libs_zip_path" {
  description = "Path to the packaged glue/ + quality/ libraries. Build with `make package`."
  type        = string
}

variable "libs_zip_hash" {
  description = "Content hash of the libs zip; changing it forces a new S3 key and a job update."
  type        = string
}

variable "lake_bucket_id" {
  type = string
}

variable "lake_bucket_arn" {
  type = string
}

variable "kms_key_arn" {
  type    = string
  default = null
}

variable "databases" {
  description = "Glue databases the jobs may touch."
  type        = list(string)
}

# --------------------------------------------------------------------- table names

variable "bronze_table" {
  type    = string
  default = "fraud_bronze.transactions"
}

variable "silver_table" {
  type    = string
  default = "fraud_silver.transactions"
}

variable "merchant_dim_table" {
  type    = string
  default = "fraud_silver.merchant_dim"
}

variable "fraud_metrics_table" {
  type    = string
  default = "fraud_gold.fraud_metrics_daily"
}

variable "merchant_risk_table" {
  type    = string
  default = "fraud_gold.merchant_risk"
}

variable "scores_table" {
  type    = string
  default = "fraud_gold.transaction_risk_scores"
}

variable "metrics_table" {
  type    = string
  default = "fraud_gold.model_metrics"
}

variable "value_table" {
  type    = string
  default = "fraud_gold.fraud_value_daily"
}

# ------------------------------------------------------------------- cost controls

variable "glue_version" {
  description = "Glue version. 5.0 = Spark 3.5, which the transforms are written against."
  type        = string
  default     = "5.0"
}

variable "worker_type" {
  description = "Smallest Spark worker that demonstrates the concept."
  type        = string
  default     = "G.1X"

  validation {
    condition     = contains(["G.1X", "G.2X"], var.worker_type)
    error_message = "Use G.1X for this project. G.2X doubles the DPU cost per worker."
  }
}

variable "number_of_workers" {
  description = "Worker count. Capped at 2 by the project's cost rules."
  type        = number
  default     = 2

  validation {
    condition     = var.number_of_workers <= 2
    error_message = "The project cost rules cap Glue jobs at 2 workers."
  }
}

variable "job_timeout_minutes" {
  description = "Hard timeout. A hung job cannot bill for a full day."
  type        = number
  default     = 15

  validation {
    condition     = var.job_timeout_minutes <= 15
    error_message = "The project cost rules cap Glue job timeouts at 15 minutes."
  }
}

variable "log_retention_days" {
  type    = number
  default = 7
}
