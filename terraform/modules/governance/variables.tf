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

variable "lake_bucket_arn" {
  type = string
}

variable "athena_workgroup" {
  type    = string
  default = "fraud-lake"
}

variable "gold_database" {
  type    = string
  default = "fraud_gold"
}

variable "fraud_metrics_table" {
  type    = string
  default = "fraud_metrics_daily"
}

variable "merchant_risk_table" {
  type    = string
  default = "merchant_risk"
}

# Columns the `analyst` persona may not see, enforced at the catalog rather than by
# query convention — the exclusion holds even for `SELECT *`. Per-table lists because
# Lake Formation rejects a grant that excludes a column the table does not have.

variable "metrics_excluded_columns" {
  description = "Excluded from analyst on fraud_metrics_daily."
  type        = list(string)
  default     = ["distinct_customer_count", "total_fraud_signals"]
}

variable "risk_excluded_columns" {
  description = "Excluded from analyst on merchant_risk."
  type        = list(string)
  default     = ["distinct_customer_count", "merchant_risk_score"]
}

variable "enable_lake_formation" {
  description = <<-EOT
    Register the lake with Lake Formation and create the two personas.
    Free, but it changes how access works: once a location is registered, IAM alone no
    longer grants read access. Enable after the pipeline has run at least once.
  EOT
  type        = bool
  default     = false
}

variable "lake_formation_service_role_arn" {
  description = "Role Lake Formation assumes to access the registered location. Null uses the AWS service-linked role."
  type        = string
  default     = null
}

variable "enable_cloudtrail" {
  description = <<-EOT
    Create the audit trail.
    COST: the first management-event trail is free; S3 data events are per-event and are
    scoped here to the lake bucket only.
  EOT
  type        = bool
  default     = false
}

variable "trail_retention_days" {
  type    = number
  default = 30
}

variable "force_destroy_buckets" {
  type    = bool
  default = true
}

variable "lake_formation_admin_arn" {
  description = "Principal registered as the Lake Formation data lake administrator. Required to create grants."
  type        = string
  default     = ""
}

variable "glue_role_arn" {
  description = "Glue job role — needs explicit LF ALL on gold tables once defaults are revoked."
  type        = string
  default     = ""
}

variable "ml_gold_tables" {
  description = "Gold tables the ML job writes; the pipeline needs ALL, the agent SELECT."
  type        = list(string)
  default     = ["transaction_risk_scores", "model_metrics", "fraud_value_daily"]
}

variable "agent_role_arn" {
  description = "Agent task role — SELECT on gold tables. Empty when the agent layer is off."
  type        = string
  default     = ""
}
