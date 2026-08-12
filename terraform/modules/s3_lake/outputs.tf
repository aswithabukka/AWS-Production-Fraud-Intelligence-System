output "bucket_id" {
  description = "Name of the lake bucket."
  value       = aws_s3_bucket.lake.id
}

output "bucket_arn" {
  description = "ARN of the lake bucket."
  value       = aws_s3_bucket.lake.arn
}

output "kms_key_arn" {
  description = "CMK ARN when use_kms_cmk is true, otherwise null (SSE-S3 in use)."
  value       = var.use_kms_cmk ? aws_kms_key.lake[0].arn : null
}

output "raw_prefix" {
  description = "S3 URI of the raw landing zone."
  value       = "s3://${aws_s3_bucket.lake.id}/raw/transactions/"
}

output "athena_results_uri" {
  description = "S3 URI Athena writes query results to."
  value       = "s3://${aws_s3_bucket.lake.id}/athena-results/"
}
