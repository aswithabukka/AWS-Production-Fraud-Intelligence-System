# One CloudWatch dashboard covering the pipeline and the agent together.
#
# COST: the first 3 dashboards are free; beyond that it is ~$3/month each. Hence exactly
# one dashboard. The point of combining pipeline and agent metrics on a single pane is
# not tidiness — it is that "the agent gave a stale answer" and "the pipeline has not run
# since 02:00" are the same incident, and two dashboards hide that.

locals {
  pipeline_ns = "fraud-lake/pipeline"
  quality_ns  = "fraud-lake/quality"
  agent_ns    = "fraud-lake/agent"
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.name_prefix

  dashboard_body = jsonencode({
    widgets = [
      # ------------------------------------------------------------ row 1: pipeline
      {
        type = "metric"
        x    = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Rows written by layer"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 3600
          metrics = [
            [local.pipeline_ns, "BronzeRowsWritten", { label = "bronze" }],
            [".", "SilverRowsWritten", { label = "silver" }],
            [".", "GoldTransactionsAggregated", { label = "gold (aggregated)" }],
          ]
        }
      },
      {
        type = "metric"
        x    = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Quarantine rate"
          region = var.region
          view   = "timeSeries"
          stat   = "Average"
          period = 3600
          metrics = [
            [local.pipeline_ns, "QuarantineRatePct", { label = "% quarantined" }],
          ]
          yAxis = { left = { min = 0, max = 100 } }
          annotations = {
            horizontal = [{
              label = "investigate above 5%"
              value = 5
            }]
          }
        }
      },

      # ------------------------------------------------------------- row 2: quality
      {
        type = "metric"
        x    = 0, y = 6, width = 8, height = 6
        properties = {
          title  = "Data-quality pass rate by layer"
          region = var.region
          view   = "timeSeries"
          stat   = "Minimum"
          period = 3600
          metrics = [
            [local.quality_ns, "DataQualityPassRatePct", "Layer", "bronze"],
            ["...", "silver"],
          ]
          yAxis = { left = { min = 0, max = 100 } }
        }
      },
      {
        type = "metric"
        x    = 8, y = 6, width = 8, height = 6
        properties = {
          title  = "Step Functions outcomes"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 3600
          metrics = [
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", var.state_machine_arn, { label = "succeeded", color = "#2ca02c" }],
            [".", "ExecutionsFailed", ".", ".", { label = "failed", color = "#d62728" }],
            [".", "ExecutionsTimedOut", ".", ".", { label = "timed out", color = "#ff7f0e" }],
          ]
        }
      },
      {
        type = "metric"
        x    = 16, y = 6, width = 8, height = 6
        properties = {
          title  = "Pipeline freshness (hours since a bronze write)"
          region = var.region
          view   = "singleValue"
          stat   = "Sum"
          period = 10800
          metrics = [
            [local.pipeline_ns, "BronzeRowsWritten", { label = "rows in the last 3h" }],
          ]
        }
      },

      # ---------------------------------------------------------------- row 3: glue
      {
        type = "metric"
        x    = 0, y = 12, width = 12, height = 6
        properties = {
          title  = "Glue job duration"
          region = var.region
          view   = "timeSeries"
          stat   = "Average"
          period = 3600
          metrics = [
            for job in var.glue_job_names :
            ["AWS/Glue", "glue.driver.aggregate.elapsedTime", "JobName", job, "JobRunId", "ALL", { label = job }]
          ]
        }
      },
      {
        type = "metric"
        x    = 12, y = 12, width = 12, height = 6
        properties = {
          title  = "Fraud signal recall vs injected ground truth"
          region = var.region
          view   = "timeSeries"
          stat   = "Average"
          period = 3600
          metrics = [
            [local.pipeline_ns, "FraudSignalRecallPct", { label = "% of injected fraud caught by a signal" }],
            [".", "GoldFraudRatePct", { label = "observed fraud rate %" }],
          ]
        }
      },

      # --------------------------------------------------------------- row 4: agent
      {
        type = "metric"
        x    = 0, y = 18, width = 8, height = 6
        properties = {
          title  = "Agent latency (p50 / p99)"
          region = var.region
          view   = "timeSeries"
          period = 300
          metrics = [
            [local.agent_ns, "AgentLatencyMs", { stat = "p50", label = "p50" }],
            ["...", { stat = "p99", label = "p99" }],
          ]
        }
      },
      {
        type = "metric"
        x    = 8, y = 18, width = 8, height = 6
        properties = {
          title  = "Bedrock token usage"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 3600
          metrics = [
            [local.agent_ns, "AgentInputTokens", { label = "input" }],
            [".", "AgentOutputTokens", { label = "output" }],
          ]
        }
      },
      {
        type = "metric"
        x    = 16, y = 18, width = 8, height = 6
        properties = {
          title  = "Tool-call distribution"
          region = var.region
          view   = "pie"
          stat   = "Sum"
          period = 86400
          metrics = [
            [local.agent_ns, "ToolCalls", "Tool", "query_lakehouse"],
            ["...", "search_policies"],
            ["...", "pipeline_status"],
          ]
        }
      },

      # ----------------------------------------------------------- row 5: loop health
      {
        type = "metric"
        x    = 0, y = 24, width = 12, height = 6
        properties = {
          title  = "Agent iterations per question"
          region = var.region
          view   = "timeSeries"
          period = 300
          metrics = [
            [local.agent_ns, "AgentIterations", { stat = "Average", label = "mean" }],
            ["...", { stat = "Maximum", label = "max" }],
          ]
          annotations = {
            horizontal = [{
              label = "MAX_ITERATIONS — sustained contact here means the router is looping"
              value = var.max_iterations
            }]
          }
        }
      },
      {
        type = "log"
        x    = 12, y = 24, width = 12, height = 6
        properties = {
          title  = "Recent pipeline errors"
          region = var.region
          query  = "SOURCE '${var.glue_log_group}' | fields @timestamp, @message | filter @message like /(?i)(error|failed|quarantined)/ | sort @timestamp desc | limit 25"
          view   = "table"
        }
      },
    ]
  })
}
