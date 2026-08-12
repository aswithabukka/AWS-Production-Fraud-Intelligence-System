# Terraform remote state backend. Applied once, before envs/dev, and kept afterwards —
# it holds the state file, so `make destroy` deliberately does not touch it.
#
# COST: both resources are per-request priced. A state file is a few KB and lock
# writes are a handful per apply — this is cents per year, not per month.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.profile

  default_tags {
    tags = {
      Project    = "fraud-lake"
      Env        = "shared"
      CostCenter = "portfolio"
      ManagedBy  = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket = "fraud-lake-tfstate-${data.aws_caller_identity.current.account_id}"

  # No force_destroy: losing the state bucket by accident is worse than a manual
  # cleanup step. This is the one bucket in the project that is not disposable.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    # State file history is the only way back from a corrupted apply.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

resource "aws_dynamodb_table" "lock" {
  name = "fraud-lake-tflock"

  # PAY_PER_REQUEST, not provisioned: provisioned capacity has an hourly floor.
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "profile" {
  type    = string
  default = "fraud-lake"
}

output "state_bucket" {
  description = "Paste into the backend block in terraform/envs/dev/versions.tf."
  value       = aws_s3_bucket.state.id
}

output "lock_table" {
  description = "Paste into the backend block in terraform/envs/dev/versions.tf."
  value       = aws_dynamodb_table.lock.name
}
