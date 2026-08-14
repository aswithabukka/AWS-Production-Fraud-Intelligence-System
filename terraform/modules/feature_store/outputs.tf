output "table_name" {
  description = "Set as FEATURES_TABLE for the /score endpoint."
  value       = aws_dynamodb_table.features.name
}

output "table_arn" {
  value = aws_dynamodb_table.features.arn
}
