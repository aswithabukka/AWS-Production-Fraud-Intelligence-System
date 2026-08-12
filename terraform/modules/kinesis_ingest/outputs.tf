output "stream_name" {
  description = "Kinesis stream name — pass to the producer's --stream flag."
  value       = aws_kinesis_stream.transactions.name
}

output "stream_arn" {
  description = "Kinesis stream ARN."
  value       = aws_kinesis_stream.transactions.arn
}

output "delivery_stream_name" {
  description = "Firehose delivery stream name."
  value       = aws_kinesis_firehose_delivery_stream.raw.name
}

output "firehose_role_arn" {
  description = "IAM role Firehose assumes."
  value       = aws_iam_role.firehose.arn
}

output "log_group_name" {
  description = "CloudWatch log group for Firehose delivery errors."
  value       = aws_cloudwatch_log_group.firehose.name
}
