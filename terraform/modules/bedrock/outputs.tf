output "knowledge_base_id" {
  description = "Set as KNOWLEDGE_BASE_ID for the agent."
  value       = aws_bedrockagent_knowledge_base.policies.id
}

output "knowledge_base_arn" {
  value = aws_bedrockagent_knowledge_base.policies.arn
}

output "data_source_id" {
  description = "Needed by the kb_refresh Lambda to start an ingestion job."
  value       = aws_bedrockagent_data_source.policies.data_source_id
}

output "guardrail_id" {
  description = "Set as GUARDRAIL_ID for the agent."
  value       = aws_bedrock_guardrail.main.guardrail_id
}

output "guardrail_version" {
  value = aws_bedrock_guardrail_version.main.version
}

output "agent_role_arn" {
  description = "Task role for the ECS service running the API and MCP server."
  value       = aws_iam_role.agent.arn
}

output "vector_index_arn" {
  value = aws_s3vectors_index.policies.arn
}

output "policy_documents_uploaded" {
  value = [for obj in aws_s3_object.policy_documents : obj.key]
}
