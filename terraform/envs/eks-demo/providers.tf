terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }

  # SEPARATE STATE, deliberately. If this shared state with envs/dev, `make destroy`
  # would either take the cluster with it (surprising) or leave it behind (expensive).
  # A distinct state file makes the teardown an explicit, separate act.
  #
  # backend "s3" {
  #   bucket = "fraud-lake-tfstate-<account-id>"
  #   key    = "envs/eks-demo/terraform.tfstate"
  #   region = "us-east-1"
  #   dynamodb_table = "fraud-lake-tflock"
  #   encrypt = true
  # }
}

provider "aws" {
  region  = var.region
  profile = var.profile

  default_tags {
    tags = {
      Project    = "fraud-lake"
      Env        = "eks-demo"
      CostCenter = "portfolio"
      ManagedBy  = "terraform"
      # Makes a forgotten cluster obvious in Cost Explorer at a glance.
      Lifecycle = "SAME-DAY-TEARDOWN"
    }
  }
}
