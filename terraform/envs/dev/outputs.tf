output "lake_bucket" {
  description = "Lake bucket name."
  value       = module.lake.bucket_id
}

output "raw_prefix" {
  description = "Where Firehose lands raw events."
  value       = module.lake.raw_prefix
}

output "athena_workgroup" {
  description = "Athena workgroup with the 1 GB per-query scan cap."
  value       = module.athena.workgroup_name
}

output "glue_databases" {
  description = "Glue Data Catalog databases, one per lake zone."
  value       = module.athena.database_names
}

output "raw_table" {
  description = "Queryable immediately after the first Firehose delivery."
  value       = module.athena.raw_table
}

output "kinesis_stream_name" {
  description = "Kinesis stream name, or null when enable_stream = false."
  value       = try(module.ingest[0].stream_name, null)
}

output "firehose_delivery_stream" {
  description = "Firehose delivery stream name, or null when enable_stream = false."
  value       = try(module.ingest[0].delivery_stream_name, null)
}

output "encryption_mode" {
  description = "Which encryption the lake is using."
  value       = var.use_kms_cmk ? "SSE-KMS (customer-managed key, ~$1/month)" : "SSE-S3 (no charge)"
}

output "producer_command" {
  description = "Copy-paste command to seed the stream once applied."
  value = var.enable_stream ? join(" ", [
    "python -m ingestion.producer",
    "--stream ${try(module.ingest[0].stream_name, "")}",
    "--region ${var.region}",
    "--profile ${var.profile}",
    "--duration 120 --rate 50",
  ]) : "stream disabled — run `make stream-up` first"
}
