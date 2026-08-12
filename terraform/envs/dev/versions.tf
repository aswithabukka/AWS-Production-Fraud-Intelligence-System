terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }

  # Remote state. Commented out until `terraform/bootstrap` has been applied, because
  # the backend bucket has to exist before `terraform init` can use it.
  #
  #   1. cd terraform/bootstrap && terraform init && terraform apply
  #   2. copy the bucket + table names from its output into the block below
  #   3. uncomment, then: terraform init -migrate-state
  #
  # Both backend resources are per-request priced — a few cents a year at this volume.
  #
  # backend "s3" {
  #   bucket         = "fraud-lake-tfstate-<account-id>"
  #   key            = "envs/dev/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "fraud-lake-tflock"
  #   encrypt        = true
  # }
}
