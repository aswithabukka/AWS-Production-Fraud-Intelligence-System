output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}

output "state_machine_name" {
  value = aws_sfn_state_machine.pipeline.name
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "lambda_function_names" {
  value = { for k, fn in aws_lambda_function.fn : k => fn.function_name }
}

output "schedule_state" {
  description = "Whether the hourly trigger is armed."
  value       = aws_scheduler_schedule.hourly.state
}
