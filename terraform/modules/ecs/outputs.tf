output "repository_urls" {
  description = "Push targets for CI."
  value       = { for k, repo in aws_ecr_repository.repo : k => repo.repository_url }
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "service_name" {
  value = aws_ecs_service.api.name
}

output "task_definition_family" {
  value = aws_ecs_task_definition.api.family
}

output "alb_dns_name" {
  description = "Public URL, or null when the ALB is disabled."
  value       = var.enable_alb ? aws_lb.main[0].dns_name : null
}

output "task_security_group_id" {
  value = aws_security_group.task.id
}

output "monthly_floor_usd" {
  description = "Approximate always-on cost of this module as configured."
  value       = var.enable_alb ? 16.4 : 0
}
