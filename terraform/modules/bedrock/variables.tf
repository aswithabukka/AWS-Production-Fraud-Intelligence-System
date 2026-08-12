variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "source_root" {
  description = "Absolute path to the repo root, used to upload the policy corpus."
  type        = string
}

variable "lake_bucket_id" {
  type = string
}

variable "lake_bucket_arn" {
  type = string
}

variable "athena_workgroup" {
  description = "The workgroup the agent may query in — the one with the 1 GB scan cap."
  type        = string
  default     = "fraud-lake"
}

variable "embedding_model_id" {
  description = "Embedding model for the knowledge base. Titan V2 emits 1024 dimensions."
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dimension" {
  description = "Must match the embedding model's output dimension exactly."
  type        = number
  default     = 1024
}

variable "chunk_max_tokens" {
  description = <<-EOT
    Chunk size in tokens. Larger chunks mean fewer, longer retrieved passages: better for
    tabular policy documents where a threshold table must stay intact, worse for precision.
    300 keeps a typical policy section whole.
  EOT
  type        = number
  default     = 300
}

variable "invocable_model_arns" {
  description = <<-EOT
    The exact model ARNs the agent may invoke. Named explicitly rather than wildcarded:
    a wildcard would let a config change silently switch to a model costing far more per
    token, with no infrastructure change to review.
  EOT
  type        = list(string)
}
