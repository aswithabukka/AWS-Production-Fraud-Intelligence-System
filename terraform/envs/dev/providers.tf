provider "aws" {
  region  = var.region
  profile = var.profile

  # Every resource this stack creates is tagged here rather than in the modules.
  # Untagged spend is unattributable spend, and hand-tagging is how resources get
  # missed. This also makes `make cost` (Cost Explorer filtered on Project) work.
  default_tags {
    tags = {
      Project    = "fraud-lake"
      Env        = var.env
      CostCenter = "portfolio"
      ManagedBy  = "terraform"
    }
  }
}
