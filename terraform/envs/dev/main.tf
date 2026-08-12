data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# ------------------------------------------------------------------------ storage
# Per-request pricing. Safe to leave up indefinitely — this is what keeps the lake
# queryable after `make stream-down`.

module "lake" {
  source = "../../modules/s3_lake"

  name_prefix                   = var.name_prefix
  account_id                    = local.account_id
  use_kms_cmk                   = var.use_kms_cmk
  force_destroy                 = var.force_destroy_buckets
  raw_retention_days            = var.raw_retention_days
  athena_results_retention_days = var.athena_results_retention_days
}

# ------------------------------------------------------------------------- query
# Per-request pricing. Costs nothing when no query is running.

module "athena" {
  source = "../../modules/athena"

  workgroup_name       = var.name_prefix
  lake_bucket_id       = module.lake.bucket_id
  kms_key_arn          = module.lake.kms_key_arn
  bytes_scanned_cutoff = var.athena_scan_cutoff_bytes
  force_destroy        = var.force_destroy_buckets
}

# ------------------------------------------------------------------------ ingest
# COST GATE. The Kinesis stream bills a per-stream-hour charge whether or not anything
# is written to it (~$26–29/month if left up). `enable_stream = false` destroys the
# stream and Firehose while leaving the bucket, catalog, and workgroup intact.
#
#   make stream-down   # end of a demo session
#   make stream-up     # start of the next one
#
# See COSTS.md and docs/decisions.md D-009.

module "ingest" {
  source = "../../modules/kinesis_ingest"
  count  = var.enable_stream ? 1 : 0

  name_prefix             = var.name_prefix
  account_id              = local.account_id
  lake_bucket_arn         = module.lake.bucket_arn
  kms_key_arn             = module.lake.kms_key_arn
  buffer_size_mb          = var.firehose_buffer_size_mb
  buffer_interval_seconds = var.firehose_buffer_interval_seconds
  log_retention_days      = var.log_retention_days
}
