output "cluster_name" {
  value = aws_eks_cluster.demo.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.demo.endpoint
}

output "kubeconfig_command" {
  description = "Run this to point kubectl at the demo cluster."
  value       = "aws eks update-kubeconfig --name ${aws_eks_cluster.demo.name} --region ${var.region} --profile ${var.profile}"
}

output "teardown_command" {
  description = "RUN THIS TODAY. The control plane bills ~$73/month from the moment it exists."
  value       = "terraform -chdir=terraform/envs/eks-demo destroy"
}

output "estimated_cost_per_hour_usd" {
  description = "Control plane plus nodes."
  value       = 0.10 + (var.node_count * 0.0208)
}

output "estimated_cost_if_forgotten_monthly_usd" {
  description = "What this costs if you do not destroy it today."
  value       = floor((0.10 + (var.node_count * 0.0208)) * 24 * 30)
}
