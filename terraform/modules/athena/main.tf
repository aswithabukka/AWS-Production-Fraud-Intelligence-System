# Athena workgroup + the Glue Data Catalog databases for each lake zone.
#
# COST: Athena is per-TB-scanned and costs nothing idle. The workgroup's
# bytes_scanned_cutoff_per_query is the guardrail that matters — a runaway
# `SELECT *` against an unpartitioned prefix is the only way this line item
# gets interesting, and the cap makes that query fail instead of bill.

locals {
  # Column order matches the producer's emit order. `auth_response_code` only appears
  # once the producer runs with --schema-version 2; the JSON SerDe returns NULL for it
  # until then, which is exactly the read-schema behaviour the evolution demo needs.
  raw_columns = [
    "transaction_id",
    "customer_id",
    "merchant_id",
    "mcc",
    "amount",
    "currency",
    # `timestamp` is a reserved word in Athena/Hive DDL — a column of that name has to be
    # backtick-quoted in every query that touches it, which the SQL agent in slice 2 would
    # get wrong sooner or later. The SerDe `mapping.` parameter below renames it on read.
    "txn_timestamp",
    "lat",
    "lon",
    "device_id",
    "channel",
    "is_fraud",
    "ingest_timestamp",
    "schema_version",
    "anomaly_type",
    "auth_response_code",
  ]
}

resource "aws_athena_workgroup" "main" {
  name        = var.workgroup_name
  description = "fraud-lake queries. 1 GB per-query scan cap, enforced at the workgroup."

  configuration {
    # Clients cannot override these — that is the point of the cap.
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    # Hard ceiling: any query projected to scan more than this is cancelled.
    bytes_scanned_cutoff_per_query = var.bytes_scanned_cutoff

    result_configuration {
      output_location = "s3://${var.lake_bucket_id}/${var.results_prefix}/"

      encryption_configuration {
        encryption_option = var.kms_key_arn != null ? "SSE_KMS" : "SSE_S3"
        kms_key_arn       = var.kms_key_arn
      }
    }

    engine_version {
      # Athena engine v3 — required for Iceberg table maintenance (OPTIMIZE, VACUUM).
      selected_engine_version = "Athena engine version 3"
    }
  }

  force_destroy = var.force_destroy
}

# ------------------------------------------------------------------- catalog dbs

resource "aws_glue_catalog_database" "zones" {
  for_each = toset(var.databases)

  name        = each.value
  description = "fraud-lake ${split("_", each.value)[1]} zone"
  location_uri = each.value == var.raw_database ? "s3://${var.lake_bucket_id}/raw/" : (
    "s3://${var.lake_bucket_id}/${split("_", each.value)[1]}/"
  )
}

# --------------------------------------------------- raw external table (optional)
# Lets you query the landing zone in Athena the moment Firehose delivers, before any
# Glue job exists. Uses PARTITION PROJECTION rather than a crawler: projection is
# computed at query time and costs nothing, whereas a scheduled crawler bills per run.

resource "aws_glue_catalog_table" "raw_transactions" {
  count = var.create_raw_table ? 1 : 0

  name          = "transactions"
  database_name = aws_glue_catalog_database.zones[var.raw_database].name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"  = "json"
    "compressionType" = "gzip"
    "EXTERNAL"        = "TRUE"

    # Partition projection — no crawler, no MSCK REPAIR, no per-run charge.
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.range"         = "${var.projection_start_date},NOW"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"
    "storage.location.template"   = "s3://${var.lake_bucket_id}/raw/transactions/dt=$${dt}"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${var.lake_bucket_id}/raw/transactions/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"

      parameters = {
        # Malformed records are expected here — the quarantine demo puts them in
        # deliberately. The raw table should return what it can rather than fail the
        # whole query; rejecting bad records is bronze's job, not the raw view's.
        "ignore.malformed.json" = "true"
        "dots.in.keys"          = "false"

        # Read the JSON field `timestamp` into the column `txn_timestamp`, avoiding the
        # reserved word without touching the producer's wire format.
        "mapping.txn_timestamp" = "timestamp"
      }
    }

    # Types are deliberately loose — every column is a string. `amount` arrives as a
    # JSON number normally and as the literal "N/A" in a corrupted record; typing it as
    # string means the raw view survives both, and bronze does the cast-or-quarantine.
    # A raw zone that can fail to parse is a raw zone that hides its own bad data.
    dynamic "columns" {
      for_each = local.raw_columns

      content {
        name = columns.value
        type = "string"
      }
    }
  }
}

# Named queries: the smoke tests for slice 1a, saved in the console so the demo does
# not depend on remembering SQL. Free — they are catalog metadata.
resource "aws_athena_named_query" "raw_row_count" {
  count = var.create_raw_table ? 1 : 0

  name        = "1a-raw-row-count"
  workgroup   = aws_athena_workgroup.main.id
  database    = var.raw_database
  description = "Smoke test: rows landed per day."
  query       = <<-SQL
    SELECT dt, count(*) AS records
    FROM ${var.raw_database}.transactions
    GROUP BY dt
    ORDER BY dt DESC;
  SQL
}

resource "aws_athena_named_query" "raw_fraud_mix" {
  count = var.create_raw_table ? 1 : 0

  name        = "1a-raw-fraud-mix"
  workgroup   = aws_athena_workgroup.main.id
  database    = var.raw_database
  description = "Smoke test: fraud share and channel mix in the landing zone."
  query       = <<-SQL
    SELECT
      dt,
      channel,
      count(*)                                        AS transactions,
      sum(CASE WHEN is_fraud = 'true' THEN 1 ELSE 0 END) AS fraud_transactions,
      round(100.0 * sum(CASE WHEN is_fraud = 'true' THEN 1 ELSE 0 END) / count(*), 3) AS fraud_pct
    FROM ${var.raw_database}.transactions
    GROUP BY dt, channel
    ORDER BY dt DESC, transactions DESC;
  SQL
}
