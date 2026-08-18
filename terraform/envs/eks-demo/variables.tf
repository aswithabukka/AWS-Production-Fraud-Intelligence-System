variable "region" {
  type    = string
  default = "us-east-1"
}

variable "profile" {
  type    = string
  default = "fraud-lake"
}

variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

variable "vpc_cidr" {
  description = "Separate range from envs/dev so the two VPCs never need to peer."
  type        = string
  default     = "10.30.0.0/16"
}

variable "kubernetes_version" {
  type    = string
  default = "1.31"
}

variable "node_instance_type" {
  description = "Smallest instance that runs the demo. ARM to match the image. Do not scale this up."
  type        = string
  default     = "t4g.small"
}

variable "node_count" {
  description = "Node count. 2 is the minimum for the HPA scale-out to be visible."
  type        = number
  default     = 2

  validation {
    condition     = var.node_count <= 2
    error_message = "This is a demo cluster. More than 2 nodes multiplies an already-billing hourly floor."
  }
}

variable "allowed_api_cidrs" {
  description = "CIDRs allowed to reach the Kubernetes API. Set to your own IP."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enable_control_plane_logs" {
  description = "Control-plane logging is per-GB ingested. Off for a short demo."
  type        = bool
  default     = false
}

variable "pod_identity_addon_version" {
  description = "Leave null to take the default for the cluster version."
  type        = string
  default     = null
}

variable "agent_role_arn" {
  description = "The agent role from envs/dev, so pods get the same identity as the Fargate task."
  type        = string
}

variable "api_image" {
  description = "ECR image URI for the API, used by the k8s manifests."
  type        = string
  default     = ""
}
