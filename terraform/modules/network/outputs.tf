output "vpc_id" {
  value = aws_vpc.main.id
}

output "vpc_cidr" {
  value = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  description = "Where the ALB lives, and where tasks live in public_tasks mode."
  value       = aws_subnet.public[*].id
}

output "task_subnet_ids" {
  description = "Subnets the Fargate tasks run in, per the selected networking mode."
  value       = local.use_endpoints ? aws_subnet.private[*].id : aws_subnet.public[*].id
}

output "tasks_need_public_ip" {
  description = "True in public_tasks mode — tasks reach AWS APIs over the internet gateway."
  value       = !local.use_endpoints
}

output "networking_mode" {
  value = var.networking_mode
}

output "estimated_monthly_endpoint_cost_usd" {
  description = "Rough hourly-floor cost of this networking mode while it exists."
  value       = local.use_endpoints ? length(local.interface_services) * var.az_count * 7.3 : 0
}
