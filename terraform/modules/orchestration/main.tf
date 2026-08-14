# Step Functions state machine, its Lambdas, the schedule, and the alarms.
#
# COST: every component here is per-request. Step Functions bills per state transition
# (this pipeline is ~12 per run, and the free tier is 4,000/month), Lambda is well inside
# its free tier at three small functions, and EventBridge Scheduler is per-invocation.
# Running hourly for a month is a few thousand transitions — cents, and usually zero.

locals {
  lambda_functions = {
    quality_gate = {
      handler     = "quality_gate.handler"
      source_file = "${var.source_root}/orchestration/lambdas/quality_gate.py"
      timeout     = 30
      memory      = 256
      environment = {
        REPORT_PREFIX = "s3://${var.lake_bucket_id}/quality-reports"
      }
    }
    failure_report = {
      handler     = "failure_report.handler"
      source_file = "${var.source_root}/orchestration/lambdas/failure_report.py"
      timeout     = 30
      memory      = 256
      environment = {
        LAKE_BUCKET_URI = var.lake_bucket_id
        SNS_TOPIC_ARN   = aws_sns_topic.alerts.arn
      }
    }
    kb_refresh = {
      handler     = "kb_refresh.handler"
      source_file = "${var.source_root}/orchestration/lambdas/kb_refresh.py"
      timeout     = 60
      memory      = 128
      environment = {
        KNOWLEDGE_BASE_ID = var.knowledge_base_id
        DATA_SOURCE_ID    = var.knowledge_base_data_source_id
      }
    }
    s3_trigger = {
      handler     = "s3_trigger.handler"
      source_file = "${var.source_root}/orchestration/lambdas/s3_trigger.py"
      timeout     = 30
      memory      = 128
      environment = {
        STATE_MACHINE_ARN = "arn:aws:states:${var.region}:${var.account_id}:stateMachine:${var.name_prefix}-pipeline"
      }
    }
  }
}

# ------------------------------------------------------------------------- alerts

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ------------------------------------------------------------------------ lambdas

data "archive_file" "lambda" {
  for_each = local.lambda_functions

  type        = "zip"
  source_file = each.value.source_file
  output_path = "${path.module}/.build/${each.key}.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "Logging"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:/aws/lambda/${var.name_prefix}-*"]
  }

  # quality_gate reads reports; failure_report writes them. Both are scoped to their own
  # prefix rather than the bucket.
  statement {
    sid       = "ReadQualityReports"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.lake_bucket_arn}/quality-reports/*"]
  }

  statement {
    sid       = "WriteFailureReports"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${var.lake_bucket_arn}/failure-reports/*"]
  }

  statement {
    sid       = "PublishAlerts"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
  }

  # The S3-arrival path starts exactly one state machine, named explicitly.
  statement {
    sid       = "StartPipeline"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = ["arn:aws:states:${var.region}:${var.account_id}:stateMachine:${var.name_prefix}-pipeline"]
  }

  statement {
    sid       = "RefreshKnowledgeBase"
    effect    = "Allow"
    actions   = ["bedrock:StartIngestionJob", "bedrock:GetIngestionJob"]
    resources = ["arn:aws:bedrock:${var.region}:${var.account_id}:knowledge-base/*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name_prefix}-lambda-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.lambda_functions

  name              = "/aws/lambda/${var.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "fn" {
  for_each = local.lambda_functions

  function_name = "${var.name_prefix}-${each.key}"
  role          = aws_iam_role.lambda.arn
  handler       = each.value.handler
  runtime       = "python3.12"

  filename         = data.archive_file.lambda[each.key].output_path
  source_code_hash = data.archive_file.lambda[each.key].output_base64sha256

  # Cost rules: 128-512 MB, timeout <= 60s. Trigger and light validation only — no heavy
  # processing ever happens in a Lambda in this project.
  memory_size = each.value.memory
  timeout     = each.value.timeout

  environment {
    variables = each.value.environment
  }

  depends_on = [aws_cloudwatch_log_group.lambda]
}

# ------------------------------------------------------------------ state machine

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sfn" {
  name               = "${var.name_prefix}-stepfn-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}

data "aws_iam_policy_document" "sfn" {
  statement {
    sid       = "RunGlueJobs"
    effect    = "Allow"
    actions   = ["glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"]
    resources = [for name in var.glue_job_names : "arn:aws:glue:${var.region}:${var.account_id}:job/${name}"]
  }

  statement {
    sid       = "InvokeLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [for fn in aws_lambda_function.fn : fn.arn]
  }

  # .sync integrations need EventBridge managed rules to receive completion events.
  statement {
    sid    = "SyncIntegrationEvents"
    effect = "Allow"

    actions = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]

    resources = [
      "arn:aws:events:${var.region}:${var.account_id}:rule/StepFunctionsGetEventsForGlueJobRule",
    ]
  }

  statement {
    sid    = "Logging"
    effect = "Allow"

    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]

    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "sfn" {
  name   = "${var.name_prefix}-stepfn-policy"
  role   = aws_iam_role.sfn.id
  policy = data.aws_iam_policy_document.sfn.json
}

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${var.name_prefix}-pipeline"
  retention_in_days = var.log_retention_days
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.name_prefix}-pipeline"
  role_arn = aws_iam_role.sfn.arn

  # STANDARD, not EXPRESS: this pipeline runs for minutes, and Standard gives the full
  # visual execution history that the failure-scenario screenshots depend on.
  type = "STANDARD"

  definition = templatefile("${var.source_root}/orchestration/pipeline.asl.json", {
    bronze_job_name             = var.glue_job_names["bronze"]
    silver_job_name             = var.glue_job_names["silver"]
    gold_job_name               = var.glue_job_names["gold"]
    ml_job_name                 = var.glue_job_names["ml"]
    quality_job_name            = var.glue_job_names["quality"]
    bronze_table                = var.bronze_table
    silver_table                = var.silver_table
    quality_gate_function_arn   = aws_lambda_function.fn["quality_gate"].arn
    failure_report_function_arn = aws_lambda_function.fn["failure_report"].arn
    kb_refresh_function_arn     = aws_lambda_function.fn["kb_refresh"].arn
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = false # X-Ray is per-trace; not needed to tell this story
  }
}

# ---------------------------------------------------------------------- schedule

resource "aws_iam_role" "scheduler" {
  name = "${var.name_prefix}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "scheduler.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.name_prefix}-scheduler-policy"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.pipeline.arn
    }]
  })
}

resource "aws_scheduler_schedule" "hourly" {
  name = "${var.name_prefix}-hourly"

  # Disabled by default. An hourly schedule left running against an empty stream is a
  # pipeline that bills Glue DPU-minutes every hour to process zero rows — enable it for
  # the demo window, disable it afterwards.
  state = var.enable_schedule ? "ENABLED" : "DISABLED"

  flexible_time_window {
    # 15 minutes of slack: nothing here is latency-sensitive, and flexible windows are
    # scheduled when capacity is cheapest.
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 15
  }

  schedule_expression          = "rate(1 hour)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_sfn_state_machine.pipeline.arn
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      trigger      = "schedule"
      process_date = ""
    })

    retry_policy {
      maximum_retry_attempts = 1
    }
  }
}

# --------------------------------------------------------- event-driven trigger

resource "aws_lambda_permission" "s3_invoke" {
  count = var.enable_s3_trigger ? 1 : 0

  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.fn["s3_trigger"].function_name
  principal      = "s3.amazonaws.com"
  source_arn     = var.lake_bucket_arn
  source_account = var.account_id
}

resource "aws_s3_bucket_notification" "raw_arrival" {
  count = var.enable_s3_trigger ? 1 : 0

  bucket = var.lake_bucket_id

  lambda_function {
    lambda_function_arn = aws_lambda_function.fn["s3_trigger"].arn
    events              = ["s3:ObjectCreated:*"]

    # Scoped to the raw prefix. Without this the pipeline triggers on its own Glue and
    # Athena output — an infinite loop that bills real money.
    filter_prefix = "raw/transactions/"
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}

# ------------------------------------------------------------------------ alarms

resource "aws_cloudwatch_metric_alarm" "execution_failed" {
  alarm_name          = "${var.name_prefix}-pipeline-execution-failed"
  alarm_description   = "A pipeline execution reached the Fail state."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.pipeline.arn
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "quality_gate_failed" {
  for_each = toset(["bronze", "silver"])

  alarm_name          = "${var.name_prefix}-quality-gate-failed-${each.value}"
  alarm_description   = "The ${each.value} data-quality gate returned a failing verdict."
  namespace           = "fraud-lake/quality"
  metric_name         = "DataQualityGatePassed"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Missing data here means the gate did not run, which the execution-failed alarm
  # already covers. Alarming twice on one incident just trains you to ignore alerts.
  treat_missing_data = "notBreaching"

  dimensions = {
    Layer = each.value
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "pipeline_freshness" {
  alarm_name        = "${var.name_prefix}-pipeline-stale"
  alarm_description = "No successful bronze write in ${var.freshness_hours} hours."

  namespace   = "fraud-lake/pipeline"
  metric_name = "BronzeRowsWritten"
  statistic   = "Sum"

  period              = var.freshness_hours * 3600
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # The one alarm where missing data MUST breach: "the pipeline stopped running entirely"
  # produces no datapoints at all, and treating that as healthy would make this alarm
  # silent in precisely the case it exists for.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# Glue failures surface as events rather than metrics — the event carries the error
# message, whereas a metric only carries a count.
resource "aws_cloudwatch_event_rule" "glue_job_failed" {
  name        = "${var.name_prefix}-glue-job-failed"
  description = "Any fraud-lake Glue job entering FAILED, TIMEOUT, or ERROR."

  event_pattern = jsonencode({
    source        = ["aws.glue"]
    "detail-type" = ["Glue Job State Change"]
    detail = {
      jobName = values(var.glue_job_names)
      state   = ["FAILED", "TIMEOUT", "ERROR"]
    }
  })
}

resource "aws_cloudwatch_event_target" "glue_job_failed" {
  rule      = aws_cloudwatch_event_rule.glue_job_failed.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      job     = "$.detail.jobName"
      state   = "$.detail.state"
      message = "$.detail.message"
      runId   = "$.detail.jobRunId"
    }

    input_template = "\"fraud-lake Glue job <job> entered <state> (run <runId>): <message>\""
  }
}

data "aws_iam_policy_document" "sns_policy" {
  statement {
    sid     = "AllowEventBridgePublish"
    effect  = "Allow"
    actions = ["sns:Publish"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "cloudwatch.amazonaws.com"]
    }

    resources = [aws_sns_topic.alerts.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.sns_policy.json
}
