terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }

  # Remote state, created by terraform/bootstrap. Values live in backend.hcl
  # (gitignored — it carries the account id): terraform init -backend-config=backend.hcl
  # Copy backend.hcl.example and fill in your account id.
  backend "s3" {}
}
