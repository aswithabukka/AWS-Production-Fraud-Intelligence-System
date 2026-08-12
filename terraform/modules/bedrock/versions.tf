terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # The aws_s3vectors_* resources require provider 6.x. This module raises the floor
      # for the whole configuration — run `terraform init -upgrade` when adding it.
      version = ">= 6.0"
    }
  }
}
