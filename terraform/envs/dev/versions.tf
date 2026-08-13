terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }

  # Remote state, created by terraform/bootstrap. Per-request priced — cents a year.
  backend "s3" {
    bucket         = "fraud-lake-tfstate-434661699277"
    key            = "envs/dev/terraform.tfstate"
    region         = "us-east-1"
    profile        = "fraud-lake"
    dynamodb_table = "fraud-lake-tflock"
    encrypt        = true
  }
}
