output "job_names" {
  description = "Map of layer -> Glue job name."
  value       = { for k, job in aws_glue_job.job : k => job.name }
}

output "role_arn" {
  description = "IAM role the Glue jobs assume."
  value       = aws_iam_role.glue.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.glue.name
}

output "quality_report_prefix" {
  description = "Where the quality gate writes its verdict."
  value       = "s3://${var.lake_bucket_id}/quality-reports"
}
