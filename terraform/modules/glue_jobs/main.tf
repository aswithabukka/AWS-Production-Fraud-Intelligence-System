# Glue PySpark jobs for bronze / silver / gold plus the data-quality gate.
#
# COST: Glue bills per DPU-hour of actual run time with a 1-minute minimum. Two G.1X
# workers for ~5 minutes is a few cents. There is no idle cost — a Glue job that never
# runs bills nothing — so these are safe to leave defined. The controls that matter are
# the worker count and the timeout, both capped below.

locals {
  # One shared library archive rather than a copy of the package per job: the transforms
  # module is imported by all four, and duplicating it is how bronze and silver end up
  # running different versions of the same function.
  libs_key = "artifacts/glue/${var.libs_zip_hash}/glue_libs.zip"

  jobs = {
    bronze = {
      script  = "bronze_job.py"
      timeout = var.job_timeout_minutes
      args = {
        "--raw_path"        = "s3://${var.lake_bucket_id}/raw/transactions"
        "--bronze_table"    = var.bronze_table
        "--quarantine_path" = "s3://${var.lake_bucket_id}/quarantine/transactions"
      }
    }
    silver = {
      script  = "silver_job.py"
      timeout = var.job_timeout_minutes
      args = {
        "--bronze_table"       = var.bronze_table
        "--silver_table"       = var.silver_table
        "--merchant_dim_table" = var.merchant_dim_table
      }
    }
    gold = {
      script  = "gold_job.py"
      timeout = var.job_timeout_minutes
      args = {
        "--silver_table"        = var.silver_table
        "--fraud_metrics_table" = var.fraud_metrics_table
        "--merchant_risk_table" = var.merchant_risk_table
      }
    }
    quality = {
      script  = "quality_job.py"
      timeout = var.job_timeout_minutes
      args = {
        # layer and target_table are supplied per-invocation by the state machine, so one
        # job definition serves all three gates instead of three near-identical copies.
        "--layer"        = "bronze"
        "--target_table" = var.bronze_table
        "--report_path"  = "s3://${var.lake_bucket_id}/quality-reports"
      }
    }
  }
}

# ------------------------------------------------------------------- job artifacts

resource "aws_s3_object" "libs" {
  bucket = var.lake_bucket_id
  key    = local.libs_key
  source = var.libs_zip_path
  etag   = filemd5(var.libs_zip_path)
}

resource "aws_s3_object" "scripts" {
  for_each = local.jobs

  bucket = var.lake_bucket_id
  key    = "artifacts/glue/scripts/${each.value.script}"
  source = "${var.source_root}/${each.key == "quality" ? "quality" : "glue"}/${each.value.script}"
  etag   = filemd5("${var.source_root}/${each.key == "quality" ? "quality" : "glue"}/${each.value.script}")
}

# --------------------------------------------------------------------------- IAM

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "glue" {
  name               = "${var.name_prefix}-glue-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "glue" {
  # Data access. Delete is required — Iceberg rewrites manifests and expires snapshots,
  # which removes objects. Scoped to the lake bucket, never account-wide.
  statement {
    sid    = "LakeDataAccess"
    effect = "Allow"

    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]

    resources = ["${var.lake_bucket_arn}/*"]
  }

  statement {
    sid       = "ListLakeBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
    resources = [var.lake_bucket_arn]
  }

  # Catalog access, scoped to this project's databases rather than the whole catalog.
  statement {
    sid    = "GlueCatalogAccess"
    effect = "Allow"

    actions = [
      "glue:GetDatabase",
      "glue:GetDatabases",
      "glue:CreateTable",
      "glue:UpdateTable",
      "glue:DeleteTable",
      "glue:GetTable",
      "glue:GetTables",
      "glue:GetPartition",
      "glue:GetPartitions",
      "glue:BatchCreatePartition",
      "glue:BatchGetPartition",
      "glue:CreatePartition",
      "glue:UpdatePartition",
      "glue:DeletePartition",
      "glue:BatchDeletePartition",
    ]

    resources = concat(
      ["arn:aws:glue:${var.region}:${var.account_id}:catalog"],
      [for db in var.databases : "arn:aws:glue:${var.region}:${var.account_id}:database/${db}"],
      [for db in var.databases : "arn:aws:glue:${var.region}:${var.account_id}:table/${db}/*"],
    )
  }

  # Glue Data Quality result publishing.
  statement {
    sid    = "DataQuality"
    effect = "Allow"

    actions = [
      "glue:GetDataQualityResult",
      "glue:PublishDataQuality",
      "glue:StartDataQualityRuleRecommendationRun",
      "glue:GetDataQualityRuleRecommendationRun",
    ]

    resources = ["*"]
  }

  statement {
    sid    = "Logging"
    effect = "Allow"

    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:AssociateKmsKey",
    ]

    resources = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws-glue/*"]
  }

  # PutMetricData has no resource-level permissions, so the namespace condition is the
  # only way to scope it. Without the condition this would be a write-any-metric grant.
  statement {
    sid       = "PublishCustomMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringLike"
      variable = "cloudwatch:namespace"
      values   = ["fraud-lake/*"]
    }
  }

  dynamic "statement" {
    for_each = var.kms_key_arn != null ? [1] : []

    content {
      sid       = "UseLakeKey"
      effect    = "Allow"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
      resources = [var.kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "glue" {
  name   = "${var.name_prefix}-glue-policy"
  role   = aws_iam_role.glue.id
  policy = data.aws_iam_policy_document.glue.json
}

resource "aws_cloudwatch_log_group" "glue" {
  name              = "/aws-glue/jobs/${var.name_prefix}"
  retention_in_days = var.log_retention_days
}

# -------------------------------------------------------------------------- jobs

resource "aws_glue_job" "job" {
  for_each = local.jobs

  name     = "${var.name_prefix}-${each.key}"
  role_arn = aws_iam_role.glue.arn

  glue_version = var.glue_version

  # COST CONTROL, enforced here rather than by convention.
  worker_type       = var.worker_type       # G.1X — the smallest Spark worker
  number_of_workers = var.number_of_workers # 2 — never more
  timeout           = each.value.timeout    # minutes; a hung job cannot bill all day

  # A failed job should surface, not silently retry and bill twice. Step Functions owns
  # retry policy so it is visible in the execution graph.
  max_retries = 0

  execution_property {
    # One run at a time. Two concurrent runs of the same job would race on the same
    # Iceberg snapshot and one would lose its commit.
    max_concurrent_runs = 1
  }

  command {
    name            = "glueetl"
    python_version  = "3"
    script_location = "s3://${var.lake_bucket_id}/${aws_s3_object.scripts[each.key].key}"
  }

  default_arguments = merge(
    {
      "--job-language" = "python"

      # Puts the Iceberg runtime on the classpath. Setting the Spark catalog properties
      # without this is the most common "ClassNotFoundException: SparkCatalog" on Glue.
      "--datalake-formats" = "iceberg"

      "--extra-py-files" = "s3://${var.lake_bucket_id}/${aws_s3_object.libs.key}"
      "--warehouse_uri"  = "s3://${var.lake_bucket_id}/warehouse"

      "--enable-auto-scaling"              = "true"
      "--enable-metrics"                   = "true"
      "--enable-continuous-cloudwatch-log" = "true"
      "--enable-observability-metrics"     = "true"
      "--continuous-log-logGroup"          = aws_cloudwatch_log_group.glue.name
      "--job-bookmark-option"              = "job-bookmark-disable"

      # Spark UI event logs are useful when a job is slow, and cost only the S3 storage
      # the lifecycle rule already expires.
      "--enable-spark-ui"       = "true"
      "--spark-event-logs-path" = "s3://${var.lake_bucket_id}/spark-logs/"
      "--TempDir"               = "s3://${var.lake_bucket_id}/glue-temp/"
    },
    each.value.args,
  )
}
