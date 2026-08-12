output "analyst_role_arn" {
  value = var.enable_lake_formation ? aws_iam_role.analyst[0].arn : null
}

output "risk_analyst_role_arn" {
  value = var.enable_lake_formation ? aws_iam_role.risk_analyst[0].arn : null
}

output "analyst_excluded_columns" {
  description = "The columns the persona demo shows disappearing."
  value       = var.analyst_excluded_columns
}

output "cloudtrail_bucket" {
  value = var.enable_cloudtrail ? aws_s3_bucket.trail[0].id : null
}

output "cloudtrail_name" {
  value = var.enable_cloudtrail ? aws_cloudtrail.main[0].name : null
}
