# Online feature store: DynamoDB rolling per-customer state + the Kinesis consumer
# Lambda that maintains it.
#
# COST: DynamoDB on-demand (per-request, $0 idle) + Lambda per-invoke. The event source
# mapping only exists while the stream does, so `make stream-down` silences the whole
# path automatically.

resource "aws_dynamodb_table" "features" {
  name         = "${var.name_prefix}-customer-features"
  billing_mode = "PAY_PER_REQUEST" # on-demand: the only defensible mode for bursty demo traffic
  hash_key     = "customer_id"

  attribute {
    name = "customer_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true # silent customers age out; the table cannot grow forever
  }
}

data "archive_file" "updater" {
  type        = "zip"
  source_file = "${var.source_root}/ingestion/feature_updater.py"
  output_path = "${path.module}/.build/feature_updater.zip"
}

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "updater" {
  name               = "${var.name_prefix}-feature-updater-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "updater" {
  statement {
    sid       = "ConsumeStream"
    effect    = "Allow"
    actions   = ["kinesis:GetRecords", "kinesis:GetShardIterator", "kinesis:DescribeStream", "kinesis:ListShards", "kinesis:DescribeStreamSummary"]
    resources = [var.stream_arn]
  }

  statement {
    sid       = "WriteFeatures"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.features.arn]
  }

  statement {
    sid       = "Logs"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/lambda/${var.name_prefix}-feature-updater*"]
  }
}

resource "aws_iam_role_policy" "updater" {
  name   = "${var.name_prefix}-feature-updater-policy"
  role   = aws_iam_role.updater.id
  policy = data.aws_iam_policy_document.updater.json
}

resource "aws_lambda_function" "updater" {
  function_name = "${var.name_prefix}-feature-updater"
  role          = aws_iam_role.updater.arn
  handler       = "feature_updater.handler"
  runtime       = "python3.12"

  filename         = data.archive_file.updater.output_path
  source_code_hash = data.archive_file.updater.output_base64sha256

  memory_size = 256
  timeout     = 60

  environment {
    variables = {
      FEATURES_TABLE = aws_dynamodb_table.features.name
    }
  }
}

# The tap on the stream. Batches generously and starts at LATEST: the store describes
# the present; history is silver's job.
resource "aws_lambda_event_source_mapping" "stream" {
  event_source_arn                   = var.stream_arn
  function_name                      = aws_lambda_function.updater.arn
  starting_position                  = "LATEST"
  batch_size                         = 200
  maximum_batching_window_in_seconds = 10
}
