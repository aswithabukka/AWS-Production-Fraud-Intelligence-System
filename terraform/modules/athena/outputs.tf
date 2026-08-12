output "workgroup_name" {
  description = "Athena workgroup name — pass to StartQueryExecution as WorkGroup."
  value       = aws_athena_workgroup.main.name
}

output "database_names" {
  description = "Glue Data Catalog databases created, one per zone."
  value       = [for db in aws_glue_catalog_database.zones : db.name]
}

output "raw_table" {
  description = "Fully-qualified raw external table, or null when not created."
  value       = var.create_raw_table ? "${var.raw_database}.transactions" : null
}

output "results_uri" {
  description = "Where Athena writes results."
  value       = "s3://${var.lake_bucket_id}/${var.results_prefix}/"
}
