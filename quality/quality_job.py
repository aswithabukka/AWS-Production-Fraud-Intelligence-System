"""Data-quality gate — evaluates a DQDL ruleset against a layer and publishes a verdict.

Runs as a Glue job. Writes three things:

  1. a JSON report to s3://.../quality-reports/<layer>/<run>/report.json
  2. CloudWatch custom metrics (pass rate, failed rule count)
  3. an exit status

The state machine does NOT branch on the exit status. It branches on the report, read
back by the `quality_gate` Lambda, because a Choice state on `$.quality.passed` renders
as a visible fork in the execution graph, whereas a failed task renders as an error —
and "the gate correctly stopped the pipeline" is a success of the design, not an error.
That distinction is the whole point of the corrupted-batch exercise.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import boto3
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from awsgluedq.transforms import EvaluateDataQuality
from pyspark.context import SparkContext
from pyspark.sql import functions as F

from glue.spark_utils import CATALOG, get_logger, iceberg_spark_conf
from quality.rulesets import RULESETS

REQUIRED_ARGS = [
    "JOB_NAME",
    "warehouse_uri",
    "layer",  # bronze | silver | gold
    "target_table",
    "report_path",
]

logger = get_logger("quality")


def main() -> None:
    args = getResolvedOptions(sys.argv, REQUIRED_ARGS)
    layer = args["layer"]

    if layer not in RULESETS:
        raise ValueError(f"unknown layer {layer!r}; expected one of {sorted(RULESETS)}")

    sc = SparkContext.getOrCreate()
    glue_context = GlueContext(sc)
    spark = glue_context.spark_session
    for key, value in iceberg_spark_conf(args["warehouse_uri"]).items():
        spark.conf.set(key, value)

    job = Job(glue_context)
    job.init(args["JOB_NAME"], args)

    table = f"{CATALOG}.{args['target_table']}"
    logger.info("evaluating %s ruleset against %s", layer, table)

    df = spark.table(table)
    dyf = DynamicFrame.fromDF(df, glue_context, f"{layer}_dq")

    outcomes = EvaluateDataQuality().process_rows(
        frame=dyf,
        ruleset=RULESETS[layer],
        publishing_options={
            "dataQualityEvaluationContext": f"fraud_lake_{layer}",
            # Glue DQ can publish straight to CloudWatch — no custom metric plumbing.
            "enableDataQualityCloudWatchMetrics": True,
            "enableDataQualityResultsPublishing": True,
        },
        additional_options={"performanceTuning.caching": "CACHE_NOTHING"},
    )

    results = outcomes.toDF().select("Rule", "Outcome", "FailureReason").collect()
    report = _build_report(layer, table, results)

    _write_report(args["report_path"], layer, report)
    _emit_metrics(layer, report)

    for rule in report["failed_rules"]:
        logger.error("DQ FAILED [%s] %s :: %s", layer, rule["rule"], rule["reason"])

    logger.info(
        "%s quality: %s/%s rules passed (%.1f%%) -> %s",
        layer,
        report["passed_count"],
        report["total_rules"],
        report["pass_rate_pct"],
        "PASS" if report["passed"] else "FAIL",
    )

    # The job itself succeeds even when the data fails. A failing gate is a working gate;
    # the state machine reads the report and takes the failure branch.
    job.commit()


def _build_report(layer: str, table: str, results: list) -> dict:
    total = len(results)
    failed = [
        {"rule": row["Rule"], "reason": row["FailureReason"] or "unspecified"}
        for row in results
        if row["Outcome"] != "Passed"
    ]
    passed_count = total - len(failed)

    return {
        "layer": layer,
        "table": table,
        "evaluated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "total_rules": total,
        "passed_count": passed_count,
        "failed_count": len(failed),
        "pass_rate_pct": round(100.0 * passed_count / total, 2) if total else 0.0,
        # Any failed rule fails the gate. No partial credit: these rules were chosen
        # because each one, alone, makes the downstream layer wrong.
        "passed": len(failed) == 0,
        "failed_rules": failed,
    }


def _write_report(report_path: str, layer: str, report: dict) -> None:
    uri = f"{report_path.rstrip('/')}/{layer}/report.json"
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(report, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("wrote quality report to %s", uri)


def _emit_metrics(layer: str, report: dict) -> None:
    boto3.client("cloudwatch").put_metric_data(
        Namespace="fraud-lake/quality",
        MetricData=[
            {
                "MetricName": "DataQualityPassRatePct",
                "Dimensions": [{"Name": "Layer", "Value": layer}],
                "Value": report["pass_rate_pct"],
                "Unit": "Percent",
            },
            {
                "MetricName": "DataQualityFailedRules",
                "Dimensions": [{"Name": "Layer", "Value": layer}],
                "Value": float(report["failed_count"]),
                "Unit": "Count",
            },
            {
                "MetricName": "DataQualityGatePassed",
                "Dimensions": [{"Name": "Layer", "Value": layer}],
                "Value": 1.0 if report["passed"] else 0.0,
                "Unit": "None",
            },
        ],
    )


def summarise_quarantine(spark, quarantine_path: str) -> list[dict]:
    """Breakdown of quarantined records by rejection reason.

    Used by the failure report so an operator sees *what* was wrong, not just that
    something was. Tolerates an absent path — no quarantine data is the normal case.
    """
    try:
        df = spark.read.parquet(quarantine_path.rstrip("/"))
    except Exception:  # noqa: BLE001 - path may not exist yet
        return []

    rows = (
        df.select(F.explode(F.split("rejection_reason", ",")).alias("reason"))
        .groupBy("reason")
        .count()
        .orderBy(F.desc("count"))
        .collect()
    )
    return [{"reason": r["reason"], "count": r["count"]} for r in rows]


if __name__ == "__main__":
    main()
