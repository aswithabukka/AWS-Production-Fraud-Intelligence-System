# Kinesis Data Stream -> Firehose -> S3 raw/.
#
# COST: this is the only slice-1a component with a meaningful idle cost. An on-demand
# stream bills a per-stream-hour charge (~$0.036/hr) whether or not anything is written
# to it — roughly $26–29 if left up for a full month. The whole module is behind
# `enable_stream` in the dev environment so `make stream-down` removes the ingest path
# and leaves the lake queryable for free. See docs/decisions.md D-009.

resource "aws_kinesis_stream" "transactions" {
  name = "${var.name_prefix}-transactions"

  # On-demand: no shard-count decision, no capacity planning, scales to the demo.
  # The alternative is exactly 1 provisioned shard — never more.
  stream_mode_details {
    stream_mode = "ON_DEMAND"
  }

  # Minimum. Replay for the duplicate-delivery demo comes from the captured JSONL file,
  # not from stream retention, so paying for extended retention buys nothing here.
  retention_period = 24

  encryption_type = "KMS"
  kms_key_id      = var.kms_key_arn != null ? var.kms_key_arn : "alias/aws/kinesis"
}

# ------------------------------------------------------------------- firehose role

data "aws_iam_policy_document" "firehose_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["firehose.amazonaws.com"]
    }

    # Confused-deputy guard: only this account's Firehose may assume the role.
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "firehose" {
  name               = "${var.name_prefix}-firehose-role"
  assume_role_policy = data.aws_iam_policy_document.firehose_assume.json
}

data "aws_iam_policy_document" "firehose" {
  # Read the stream. Scoped to this stream ARN, no wildcards.
  statement {
    sid    = "ReadSourceStream"
    effect = "Allow"

    actions = [
      "kinesis:DescribeStream",
      "kinesis:DescribeStreamSummary",
      "kinesis:GetShardIterator",
      "kinesis:GetRecords",
      "kinesis:ListShards",
    ]

    resources = [aws_kinesis_stream.transactions.arn]
  }

  # Write the raw zone only — not the whole bucket.
  statement {
    sid    = "WriteRawPrefix"
    effect = "Allow"

    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetObject",
      "s3:PutObject",
      "s3:ListBucketMultipartUploads",
    ]

    resources = ["${var.lake_bucket_arn}/raw/*"]
  }

  statement {
    sid       = "DescribeBucket"
    effect    = "Allow"
    actions   = ["s3:GetBucketLocation", "s3:ListBucket"]
    resources = [var.lake_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["raw/*", "raw/"]
    }
  }

  statement {
    sid       = "WriteDeliveryLogs"
    effect    = "Allow"
    actions   = ["logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.firehose.arn}:*"]
  }

  # Only needed when the lake is encrypted with a customer-managed key.
  dynamic "statement" {
    for_each = var.kms_key_arn != null ? [1] : []

    content {
      sid       = "UseLakeKey"
      effect    = "Allow"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
      resources = [var.kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "firehose" {
  name   = "${var.name_prefix}-firehose-policy"
  role   = aws_iam_role.firehose.id
  policy = data.aws_iam_policy_document.firehose.json
}

resource "aws_cloudwatch_log_group" "firehose" {
  name              = "/aws/kinesisfirehose/${var.name_prefix}-raw"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_stream" "firehose_s3" {
  name           = "S3Delivery"
  log_group_name = aws_cloudwatch_log_group.firehose.name
}

# ---------------------------------------------------------------------- firehose

resource "aws_kinesis_firehose_delivery_stream" "raw" {
  name        = "${var.name_prefix}-raw"
  destination = "extended_s3"

  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.transactions.arn
    role_arn           = aws_iam_role.firehose.arn
  }

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = var.lake_bucket_arn

    # Hive-style dt= partitioning so Athena and the Glue bronze job can prune by day
    # without a crawler inferring the partition scheme.
    prefix              = "raw/transactions/dt=!{timestamp:yyyy-MM-dd}/"
    error_output_prefix = "raw/errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"

    # Buffering is the classic latency-vs-small-files tradeoff. 5 MB / 300 s is the
    # spec'd shape: at ~50 events/sec the 300 s window fires first, giving roughly
    # 12 files/hour rather than thousands of tiny objects for Spark to open.
    buffering_size     = var.buffer_size_mb
    buffering_interval = var.buffer_interval_seconds

    compression_format = "GZIP"

    # Deliberately NOT converting to Parquet here. Parquet conversion requires a fixed
    # Glue table schema at the landing zone, which fights the slice-1c schema-evolution
    # demo. The raw zone stays schema-agnostic; bronze is where schema is enforced.
    # See docs/decisions.md D-011.

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose.name
      log_stream_name = aws_cloudwatch_log_stream.firehose_s3.name
    }
  }

  server_side_encryption {
    # Source records arrive already encrypted from Kinesis; this covers the buffer.
    enabled = false
  }
}
