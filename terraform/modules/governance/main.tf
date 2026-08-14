# Lake Formation personas and the CloudTrail audit trail.
#
# COST:
#   - Lake Formation: free. Permissions are metadata.
#   - CloudTrail: one management-event trail is free. S3 DATA events are per-event and are
#     scoped to the lake bucket only — an account-wide data-event trail is a genuinely
#     expensive mistake, because every Athena result write and every Glue shuffle spill
#     becomes a billable event.

# ------------------------------------------------------------------ Lake Formation

# Being an IAM administrator is NOT sufficient to manage Lake Formation grants — the
# caller must be registered as a data lake administrator first. The create-default
# blocks explicitly preserve AWS's out-of-the-box behavior (IAMAllowedPrincipals gets
# ALL on new databases/tables) so enabling governance cannot silently break the Glue
# jobs' table management.
resource "aws_lakeformation_data_lake_settings" "main" {
  count = var.enable_lake_formation ? 1 : 0

  admins = [var.lake_formation_admin_arn]

  create_database_default_permissions {
    permissions = ["ALL"]
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }

  create_table_default_permissions {
    permissions = ["ALL"]
    principal   = "IAM_ALLOWED_PRINCIPALS"
  }
}

# Registering the location hands governance of this S3 prefix to Lake Formation. From
# this point, IAM alone is no longer sufficient to read it — Lake Formation grants are
# also required, which is exactly the point of the control.
resource "aws_lakeformation_resource" "lake" {
  count = var.enable_lake_formation ? 1 : 0

  # The gold data actually lives under gold/, and credential vending only works for
  # registered locations. Standard (non-hybrid) mode so Athena vends credentials for
  # any LF-permitted principal without per-principal opt-ins. Registration does not
  # block direct S3 IAM access, so the Glue jobs' writes are unaffected.
  arn      = "${var.lake_bucket_arn}/gold"
  role_arn = var.lake_formation_service_role_arn
}

# --------------------------------------------------------------------- personas

data "aws_iam_policy_document" "persona_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "analyst" {
  count = var.enable_lake_formation ? 1 : 0

  name               = "${var.name_prefix}-analyst"
  description        = "Gold-layer aggregates, with cardholder-identifying columns excluded."
  assume_role_policy = data.aws_iam_policy_document.persona_assume.json
}

resource "aws_iam_role" "risk_analyst" {
  count = var.enable_lake_formation ? 1 : 0

  name               = "${var.name_prefix}-risk-analyst"
  description        = "Full gold-layer access including fraud-specific columns."
  assume_role_policy = data.aws_iam_policy_document.persona_assume.json
}

# Both personas need the Athena and Glue API surface. Lake Formation decides which
# columns come back; IAM decides whether the query may run at all. Both are required.
data "aws_iam_policy_document" "persona_base" {
  statement {
    sid    = "RunAthenaQueries"
    effect = "Allow"

    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:GetWorkGroup",
    ]

    resources = ["arn:aws:athena:${var.region}:${var.account_id}:workgroup/${var.athena_workgroup}"]
  }

  statement {
    sid       = "ReadCatalog"
    effect    = "Allow"
    actions   = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"]
    resources = ["*"]
  }

  statement {
    sid       = "LakeFormationCredentials"
    effect    = "Allow"
    actions   = ["lakeformation:GetDataAccess"]
    resources = ["*"]
  }

  statement {
    sid    = "AthenaResults"
    effect = "Allow"
    # GetBucketLocation is required: Athena verifies the results bucket before running.
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [var.lake_bucket_arn, "${var.lake_bucket_arn}/athena-results/*"]
  }
}

resource "aws_iam_role_policy" "analyst" {
  count = var.enable_lake_formation ? 1 : 0

  name   = "${var.name_prefix}-analyst-policy"
  role   = aws_iam_role.analyst[0].id
  policy = data.aws_iam_policy_document.persona_base.json
}

resource "aws_iam_role_policy" "risk_analyst" {
  count = var.enable_lake_formation ? 1 : 0

  name   = "${var.name_prefix}-risk-analyst-policy"
  role   = aws_iam_role.risk_analyst[0].id
  policy = data.aws_iam_policy_document.persona_base.json
}

# ------------------------------------------------------------------------ grants

# analyst: column-level EXCLUDE. Every column except the restricted ones, and — this is
# the part that matters — the exclusion survives `SELECT *`. A control that depends on
# the analyst not asking for a column is not a control.
resource "aws_lakeformation_permissions" "analyst_metrics" {
  count      = var.enable_lake_formation ? 1 : 0
  depends_on = [aws_lakeformation_data_lake_settings.main]

  principal   = aws_iam_role.analyst[0].arn
  permissions = ["SELECT"]

  table_with_columns {
    database_name = var.gold_database
    name          = var.fraud_metrics_table
    # "every column except these" — exclusions apply against the wildcard.
    wildcard              = true
    excluded_column_names = var.metrics_excluded_columns
  }
}

resource "aws_lakeformation_permissions" "analyst_merchant_risk" {
  count      = var.enable_lake_formation ? 1 : 0
  depends_on = [aws_lakeformation_data_lake_settings.main]

  principal   = aws_iam_role.analyst[0].arn
  permissions = ["SELECT"]

  table_with_columns {
    database_name         = var.gold_database
    name                  = var.merchant_risk_table
    wildcard              = true
    excluded_column_names = var.risk_excluded_columns
  }
}

# The pipeline itself: once IAMAllowedPrincipals is revoked from the gold tables,
# catalog operations on them are LF-checked for every principal — including the Glue
# role that rewrites them each run. Explicit ALL keeps the gold job working.
resource "aws_lakeformation_permissions" "glue_gold" {
  for_each = var.enable_lake_formation ? toset([var.fraud_metrics_table, var.merchant_risk_table]) : toset([])

  depends_on  = [aws_lakeformation_data_lake_settings.main]
  principal   = var.glue_role_arn
  permissions = ["ALL"]

  table {
    database_name = var.gold_database
    name          = each.value
  }
}

# The agent's task role (used when the API runs on ECS rather than as the admin user).
resource "aws_lakeformation_permissions" "agent_gold" {
  for_each = var.enable_lake_formation && var.agent_role_arn != "" ? toset([var.fraud_metrics_table, var.merchant_risk_table]) : toset([])

  depends_on  = [aws_lakeformation_data_lake_settings.main]
  principal   = var.agent_role_arn
  permissions = ["SELECT"]

  table {
    database_name = var.gold_database
    name          = each.value
  }
}

# The data lake administrator can GRANT anything but implicitly holds no data
# permissions itself — discovered when revoking the account default cut off the very
# identity running the agent locally. Admins govern; access is always explicit.
resource "aws_lakeformation_permissions" "admin_gold" {
  for_each = var.enable_lake_formation ? toset([var.fraud_metrics_table, var.merchant_risk_table]) : toset([])

  depends_on  = [aws_lakeformation_data_lake_settings.main]
  principal   = var.lake_formation_admin_arn
  permissions = ["SELECT"]

  table {
    database_name = var.gold_database
    name          = each.value
  }
}

# risk_analyst: whole database, all columns. The investigative role.
resource "aws_lakeformation_permissions" "risk_analyst_database" {
  count      = var.enable_lake_formation ? 1 : 0
  depends_on = [aws_lakeformation_data_lake_settings.main]

  principal   = aws_iam_role.risk_analyst[0].arn
  permissions = ["SELECT", "DESCRIBE"]

  table {
    database_name = var.gold_database
    wildcard      = true
  }
}

# Neither persona may reach silver or bronze. Not granting is sufficient under Lake
# Formation — there is no implicit access — but an explicit absence of a grant is easy to
# mistake for an oversight, so it is stated here as a comment rather than left to be
# inferred from what is missing.

# --------------------------------------------------------------------- CloudTrail

resource "aws_s3_bucket" "trail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket        = "${var.name_prefix}-cloudtrail-${var.account_id}"
  force_destroy = var.force_destroy_buckets
}

resource "aws_s3_bucket_public_access_block" "trail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket                  = aws_s3_bucket.trail[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.trail[0].id

  rule {
    id     = "expire-trail-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.trail_retention_days
    }
  }
}

data "aws_iam_policy_document" "trail_bucket" {
  count = var.enable_cloudtrail ? 1 : 0

  statement {
    sid    = "AWSCloudTrailAclCheck"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail[0].arn]
  }

  statement {
    sid    = "AWSCloudTrailWrite"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail[0].arn}/AWSLogs/${var.account_id}/*"]

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.trail[0].id
  policy = data.aws_iam_policy_document.trail_bucket[0].json
}

resource "aws_cloudtrail" "main" {
  count = var.enable_cloudtrail ? 1 : 0

  name           = "${var.name_prefix}-trail"
  s3_bucket_name = aws_s3_bucket.trail[0].id

  # One trail, one region. Multi-region and org trails multiply event volume, and every
  # data event is billable.
  is_multi_region_trail         = false
  include_global_service_events = true
  enable_log_file_validation    = true

  event_selector {
    read_write_type = "All"

    # Management events on the first trail are free.
    include_management_events = true

    data_resource {
      type = "AWS::S3::Object"

      # SCOPED TO THE LAKE BUCKET ONLY. `arn:aws:s3` (account-wide) is the expensive
      # mistake here — it would log every object operation in the account, including every
      # Athena result write and every Spark shuffle spill, at a per-event charge.
      values = ["${var.lake_bucket_arn}/"]
    }
  }

  depends_on = [aws_s3_bucket_policy.trail]
}
