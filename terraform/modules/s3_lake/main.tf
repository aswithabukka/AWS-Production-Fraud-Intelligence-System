# The single S3 bucket that backs the whole lakehouse. One bucket with prefixes rather
# than a bucket per zone: fewer things to tag, fewer things to forget to delete, and
# Lake Formation registers a location prefix just as happily as a bucket root.

locals {
  bucket_name = "${var.name_prefix}-${var.account_id}"
}

# ------------------------------------------------------------------ optional CMK
# Default is SSE-S3 (free). A customer-managed key carries a ~$1/month floor plus
# per-request charges, so it is opt-in — see docs/decisions.md D-008.

resource "aws_kms_key" "lake" {
  count = var.use_kms_cmk ? 1 : 0

  description             = "fraud-lake S3 encryption key"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "lake" {
  count = var.use_kms_cmk ? 1 : 0

  name          = "alias/${var.name_prefix}"
  target_key_id = aws_kms_key.lake[0].key_id
}

# ---------------------------------------------------------------------- the bucket

resource "aws_s3_bucket" "lake" {
  bucket = local.bucket_name

  # dev only: lets `make destroy` succeed without a manual empty-the-bucket step.
  # This must be false in any environment holding data you care about.
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "lake" {
  bucket = aws_s3_bucket.lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id

  versioning_configuration {
    # Iceberg is already immutable-by-snapshot, but versioning protects against an
    # accidental prefix delete while the pipeline is being developed.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.use_kms_cmk ? "aws:kms" : "AES256"
      kms_master_key_id = var.use_kms_cmk ? aws_kms_key.lake[0].arn : null
    }
    # Cuts KMS request charges by ~99% when the CMK is enabled: one data key per
    # prefix rather than one per object.
    bucket_key_enabled = var.use_kms_cmk
  }
}

# TLS-only. Cheap to add, and the first thing a security reviewer looks for.
data "aws_iam_policy_document" "deny_insecure_transport" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:*"]
    resources = [aws_s3_bucket.lake.arn, "${aws_s3_bucket.lake.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "lake" {
  bucket = aws_s3_bucket.lake.id
  policy = data.aws_iam_policy_document.deny_insecure_transport.json

  depends_on = [aws_s3_bucket_public_access_block.lake]
}

# ------------------------------------------------------------------- lifecycle
# Storage is cheap but not free, and an unbounded raw zone is how a "few GB" project
# quietly becomes a few hundred.

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id

  # Raw JSON is replayable from nothing but the producer, so it has a short life.
  rule {
    id     = "expire-raw"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    expiration {
      days = var.raw_retention_days
    }
  }

  # Athena results are pure derived output — regenerate by re-running the query.
  rule {
    id     = "expire-athena-results"
    status = "Enabled"

    filter {
      prefix = "athena-results/"
    }

    expiration {
      days = var.athena_results_retention_days
    }
  }

  # Old Iceberg snapshots leave orphaned versions behind after compaction.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }

  # Failed multipart uploads bill for storage while being invisible in the console.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.lake]
}

# Prefix placeholders so the layout is visible in the console before any data lands.
resource "aws_s3_object" "zone_placeholders" {
  for_each = toset([
    "raw/transactions/",
    "bronze/",
    "silver/",
    "gold/",
    "quarantine/",
    # policies/ deliberately absent: the Knowledge Base ingests that whole prefix, and a
    # placeholder object becomes a junk document in retrieval.
    "athena-results/",
  ])

  bucket  = aws_s3_bucket.lake.id
  key     = "${each.value}.keep"
  content = "fraud-lake zone placeholder\n"
}
