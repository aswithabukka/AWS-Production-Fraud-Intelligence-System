"""Bronze job — raw JSON to a typed, deduplicated Iceberg table.

    read raw JSON
      -> enforce schema / type cast
      -> split: valid rows vs quarantine rows (with rejection_reason)
      -> dedupe on transaction_id, keeping the latest by ingest_timestamp
      -> MERGE into bronze.transactions (Iceberg, partitioned by dt)

Records failing schema or null-critical checks go to quarantine/ and do NOT enter bronze.

Run as a Glue 5.0 PySpark job with:
    --datalake-formats iceberg
    --enable-auto-scaling
    2 x G.1X workers, 15 minute timeout
"""

from __future__ import annotations

import sys

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F

from glue.spark_utils import CATALOG, create_iceberg_table, get_logger, iceberg_spark_conf, merge_into
from glue.transforms import build_bronze

REQUIRED_ARGS = [
    "JOB_NAME",
    "raw_path",
    "warehouse_uri",
    "bronze_table",
    "quarantine_path",
]

logger = get_logger("bronze")


def main() -> None:
    args = getResolvedOptions(sys.argv, REQUIRED_ARGS)
    optional = _optional_args()

    sc = SparkContext.getOrCreate()
    glue_context = GlueContext(sc)
    spark = glue_context.spark_session
    for key, value in iceberg_spark_conf(args["warehouse_uri"]).items():
        spark.conf.set(key, value)

    job = Job(glue_context)
    job.init(args["JOB_NAME"], args)

    raw_path = args["raw_path"].rstrip("/")
    partition = optional.get("process_date")
    read_path = f"{raw_path}/dt={partition}/" if partition else f"{raw_path}/"
    logger.info("reading raw JSON from %s", read_path)

    # Read every column as a string. Firehose writes whatever the producer sent, including
    # deliberately corrupted records, and letting Spark infer types here would either fail
    # the job or silently null out fields that the quarantine path is supposed to catch.
    raw = spark.read.option("mode", "PERMISSIVE").option("primitivesAsString", "true").json(read_path)

    if raw.rdd.isEmpty():
        logger.warning("no raw records at %s — nothing to do", read_path)
        _emit_metrics(spark, bronze_rows=0, quarantine_rows=0)
        job.commit()
        return

    bronze_df, quarantine_df = build_bronze(raw)
    bronze_df = bronze_df.cache()

    bronze_count = bronze_df.count()
    quarantine_count = quarantine_df.count()
    logger.info("bronze rows=%s quarantine rows=%s", bronze_count, quarantine_count)

    bronze_table = f"{CATALOG}.{args['bronze_table']}"
    if bronze_count:
        create_iceberg_table(spark, bronze_table, bronze_df, partition_by="dt")
        # MERGE, not append: at-least-once delivery plus Step Functions retries mean this
        # job will process the same batch twice eventually.
        merge_into(spark, bronze_table, bronze_df, key_columns=["transaction_id"])

    if quarantine_count:
        # Quarantine is plain Parquet, not Iceberg. It is a dead-letter zone read by a
        # human during an incident, not a table anything joins to — an Iceberg table here
        # would add catalog surface for no query benefit.
        (
            quarantine_df.drop("rejection_reasons")
            .write.mode("append")
            .partitionBy("dt")
            .parquet(args["quarantine_path"].rstrip("/"))
        )
        _log_rejection_breakdown(quarantine_df)

    _emit_metrics(spark, bronze_rows=bronze_count, quarantine_rows=quarantine_count)
    job.commit()


def _optional_args() -> dict[str, str]:
    """Glue's getResolvedOptions raises when an argument is absent, so optional
    parameters have to be probed one at a time."""
    resolved: dict[str, str] = {}
    for name in ("process_date",):
        try:
            resolved.update(getResolvedOptions(sys.argv, [name]))
        except Exception:  # noqa: BLE001 - GlueArgumentError is not importable standalone
            continue
    return resolved


def _log_rejection_breakdown(quarantine_df) -> None:
    """Print the rejection mix. This is the first thing you want during the corrupted-batch
    exercise, and reading it out of the job log is faster than querying S3."""
    breakdown = (
        quarantine_df.select(F.explode("rejection_reasons").alias("reason"))
        .groupBy("reason")
        .count()
        .orderBy(F.desc("count"))
        .collect()
    )
    for row in breakdown:
        logger.warning("quarantined: %s = %s", row["reason"], row["count"])


def _emit_metrics(spark, bronze_rows: int, quarantine_rows: int) -> None:
    """Emit counts to CloudWatch as custom metrics.

    The quality gate branches on the ruleset result, but these two numbers are what the
    freshness alarm and the dashboard are actually built on.
    """
    import boto3

    total = bronze_rows + quarantine_rows
    quarantine_pct = (100.0 * quarantine_rows / total) if total else 0.0

    boto3.client("cloudwatch").put_metric_data(
        Namespace="fraud-lake/pipeline",
        MetricData=[
            {"MetricName": "BronzeRowsWritten", "Value": float(bronze_rows), "Unit": "Count"},
            {"MetricName": "QuarantinedRows", "Value": float(quarantine_rows), "Unit": "Count"},
            {"MetricName": "QuarantineRatePct", "Value": quarantine_pct, "Unit": "Percent"},
        ],
    )
    logger.info(
        "emitted metrics: bronze=%s quarantine=%s (%.2f%%)", bronze_rows, quarantine_rows, quarantine_pct
    )


if __name__ == "__main__":
    main()
