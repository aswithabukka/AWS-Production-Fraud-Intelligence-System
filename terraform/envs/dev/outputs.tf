output "lake_bucket" {
  value = module.lake.bucket_id
}

output "raw_prefix" {
  value = module.lake.raw_prefix
}

output "athena_workgroup" {
  value = module.athena.workgroup_name
}

output "glue_databases" {
  value = module.athena.database_names
}

output "raw_table" {
  value = module.athena.raw_table
}

output "kinesis_stream_name" {
  description = "Null when enable_stream = false."
  value       = try(module.ingest[0].stream_name, null)
}

output "glue_jobs" {
  value = module.glue.job_names
}

output "state_machine_arn" {
  value = module.orchestration.state_machine_arn
}

output "alerts_topic_arn" {
  value = module.orchestration.alerts_topic_arn
}

output "schedule_state" {
  value = module.orchestration.schedule_state
}

output "dashboard_url" {
  value = module.observability.dashboard_url
}

# ------------------------------------------------------------------- agent layer

output "knowledge_base_id" {
  value = try(module.bedrock[0].knowledge_base_id, null)
}

output "guardrail_id" {
  value = try(module.bedrock[0].guardrail_id, null)
}

output "agent_role_arn" {
  description = "Pass to the eks-demo workspace so pods share the Fargate task's identity."
  value       = try(module.bedrock[0].agent_role_arn, null)
}

# -------------------------------------------------------------------- containers

output "ecr_repositories" {
  value = try(module.ecs[0].repository_urls, null)
}

output "alb_dns_name" {
  value = try(module.ecs[0].alb_dns_name, null)
}

output "networking_mode" {
  value = try(module.network[0].networking_mode, null)
}

# ------------------------------------------------------------------- governance

output "analyst_role_arn" {
  value = module.governance.analyst_role_arn
}

output "risk_analyst_role_arn" {
  value = module.governance.risk_analyst_role_arn
}

# ------------------------------------------------------ what this costs right now

output "cost_summary" {
  description = "Approximate always-on cost of the current configuration. Verify against Cost Explorer."
  value = {
    kinesis_stream_hours = var.enable_stream ? "~$26-29/month while up — make stream-down when idle" : "$0 (disabled)"
    kms_key              = var.use_kms_cmk ? "~$1/month" : "$0 (SSE-S3)"
    alb                  = var.enable_alb ? "~$16/month — billed regardless of task count" : "$0 (disabled)"
    vpc_endpoints        = try(module.network[0].estimated_monthly_endpoint_cost_usd, 0)
    fargate_tasks        = var.ecs_desired_count == 0 ? "$0 (desired_count = 0)" : "per-second while running"
    everything_else      = "per-request — S3, Glue, Athena, Step Functions, Lambda, Bedrock, S3 Vectors"
    eks                  = "not in this workspace — see terraform/envs/eks-demo (~$103/month if left up)"
  }
}
