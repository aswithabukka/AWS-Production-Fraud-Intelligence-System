# ⚠️  SAME-DAY TEARDOWN WORKSPACE  ⚠️
#
# ============================================================================
#  THIS IS THE ONLY PART OF THE PROJECT WITH A TRUE PER-HOUR FLOOR.
#
#    EKS control plane ........ ~$0.10/hr = ~$73/month, billed from the moment the
#                               cluster exists, whether or not a single pod is running.
#                               Scaling the node group to zero does NOT stop it.
#    2 x t3.small nodes ....... ~$0.042/hr = ~$30/month
#                               ------------------------------------------------
#                               ~$103/month if left up. ~$1.40 for a two-hour demo.
#
#  This is a SEPARATE Terraform workspace with its own state precisely so that
#  `make destroy` in envs/dev can never be mistaken for having torn this down.
#
#  BEFORE YOU APPLY: set a calendar reminder for the same day.
#  AFTER THE DEMO:   cd terraform/envs/eks-demo && terraform destroy
#
#  The purpose here is a screenshot and a resume line, not a running system. Everything
#  this cluster does, ECS Fargate already does for $0 at rest.
# ============================================================================

data "aws_caller_identity" "current" {}

locals {
  cluster_name = "${var.name_prefix}-demo"
}

# A dedicated VPC rather than reusing the dev one: this workspace must be destroyable
# without touching anything the day-to-day stack depends on.
module "network" {
  source = "../../modules/network"

  name_prefix = local.cluster_name
  region      = var.region
  vpc_cidr    = var.vpc_cidr

  # Public subnets, no NAT, no interface endpoints. Nodes pull images over the internet
  # gateway, which is free — a NAT here would add $32/month to a cluster that exists for
  # two hours.
  networking_mode = "public_tasks"
}

# --------------------------------------------------------------------- cluster IAM

data "aws_iam_policy_document" "cluster_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${local.cluster_name}-cluster-role"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

data "aws_iam_policy_document" "node_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.cluster_name}-node-role"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])

  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# ------------------------------------------------------------------------- cluster

resource "aws_eks_cluster" "demo" {
  name     = local.cluster_name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids              = module.network.public_subnet_ids
    endpoint_public_access  = true
    endpoint_private_access = false
    public_access_cidrs     = var.allowed_api_cidrs
  }

  access_config {
    # API mode instead of the aws-auth ConfigMap: the modern approach, and it means
    # access is managed as Terraform resources rather than by patching a ConfigMap.
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }

  # Control-plane logging is per-GB ingested. Off for a two-hour demo; the story here is
  # the deployment, not the audit trail.
  enabled_cluster_log_types = var.enable_control_plane_logs ? ["api", "audit"] : []

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

resource "aws_eks_node_group" "demo" {
  cluster_name    = aws_eks_cluster.demo.name
  node_group_name = "${local.cluster_name}-nodes"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = module.network.public_subnet_ids

  # t3.small x2 — the smallest pair that runs CoreDNS, the CNI, and the demo deployment.
  # One node cannot satisfy the HPA's scale-out, which is part of what the demo shows.
  instance_types = [var.node_instance_type]
  capacity_type  = "ON_DEMAND"
  disk_size      = 20

  scaling_config {
    desired_size = var.node_count
    min_size     = var.node_count
    max_size     = var.node_count + 1
  }

  update_config {
    max_unavailable = 1
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# EKS Pod Identity: the modern replacement for IRSA. Lets the API pod assume the same
# agent role the Fargate task uses, so the application code is byte-identical across both
# platforms — which is the actual point of running it on Kubernetes at all.
resource "aws_eks_addon" "pod_identity" {
  cluster_name  = aws_eks_cluster.demo.name
  addon_name    = "eks-pod-identity-agent"
  addon_version = var.pod_identity_addon_version

  depends_on = [aws_eks_node_group.demo]
}

resource "aws_eks_pod_identity_association" "api" {
  cluster_name    = aws_eks_cluster.demo.name
  namespace       = "fraud-lake"
  service_account = "fraud-lake-api"
  role_arn        = var.agent_role_arn

  depends_on = [aws_eks_addon.pod_identity]
}
