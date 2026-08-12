"""Gold job — silver to the aggregate tables the SQL agent queries.

    silver.transactions
      -> gold.fraud_metrics_daily   (fraud rate, approval rate, loss by mcc/channel/dt)
      -> gold.merchant_risk         (per-merchant rollup)

Written with `overwritePartitions`, not MERGE: a daily aggregate is a complete
recomputation of its own day. Merging individual aggregate rows would leave stale rows
behind whenever a grain combination stopped appearing.

Column names in this layer are the SQL agent's prompt surface — it introspects the Glue
Catalog and writes queries from these names with no other context. `fraud_rate_pct` is
unambiguous; `rate` is not.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F

from glue.spark_utils import (
    CATALOG,
    create_iceberg_table,
    expire_old_snapshots,
    get_logger,
    iceberg_spark_conf,
    overwrite_partitions,
)
from glue.transforms import build_fraud_metrics_daily, build_merchant_risk

REQUIRED_ARGS = [
    "JOB_NAME",
    "warehouse_uri",
    "silver_table",
    "fraud_metrics_table",
    "merchant_risk_table",
]

# Recompute a trailing window rather than only today: late-arriving records land in an
# earlier dt partition, and an aggregate that only ever touches today would never see them.
RECOMPUTE_DAYS = 7

logger = get_logger("gold")


def main() -> None:
    args = getResolvedOptions(sys.argv, REQUIRED_ARGS)

    sc = SparkContext.getOrCreate()
    glue_context = GlueContext(sc)
    spark = glue_context.spark_session
    for key, value in iceberg_spark_conf(args["warehouse_uri"]).items():
        spark.conf.set(key, value)

    job = Job(glue_context)
    job.init(args["JOB_NAME"], args)

    silver_table = f"{CATALOG}.{args['silver_table']}"
    metrics_table = f"{CATALOG}.{args['fraud_metrics_table']}"
    risk_table = f"{CATALOG}.{args['merchant_risk_table']}"

    cutoff = (datetime.now(UTC) - timedelta(days=RECOMPUTE_DAYS)).date()
    silver = spark.table(silver_table).filter(F.col("dt") >= F.lit(cutoff)).cache()

    if silver.rdd.isEmpty():
        logger.warning("no silver rows since %s — nothing to aggregate", cutoff)
        job.commit()
        return

    metrics = build_fraud_metrics_daily(silver)
    create_iceberg_table(spark, metrics_table, metrics, partition_by="dt")
    overwrite_partitions(spark, metrics_table, metrics)
    logger.info("wrote %s rows to %s", metrics.count(), metrics_table)

    # merchant_risk is a full-population snapshot rather than a per-day partition — it
    # answers "how risky is this merchant now", which has no dt grain.
    risk = build_merchant_risk(silver)
    create_iceberg_table(spark, risk_table, risk, partition_by=None)
    risk.writeTo(risk_table).overwritePartitions()
    logger.info("wrote %s rows to %s", risk.count(), risk_table)

    # COST + performance: every Iceberg snapshot pins its data files in S3. Without
    # expiry the footprint only grows, however much data is logically replaced.
    for table in (metrics_table, risk_table):
        try:
            expire_old_snapshots(spark, table, retain_days=7)
        except Exception as exc:  # noqa: BLE001 - maintenance must never fail the job
            logger.warning("snapshot expiry skipped for %s: %s", table, exc)

    _emit_metrics(metrics)
    job.commit()


def _emit_metrics(metrics) -> None:
    import boto3

    summary = metrics.agg(
        F.sum("transaction_count").alias("transactions"),
        F.sum("fraud_transaction_count").alias("fraud"),
        F.sum("fraud_loss_amount_usd").alias("loss"),
    ).collect()[0]

    transactions = float(summary["transactions"] or 0)
    fraud = float(summary["fraud"] or 0)

    boto3.client("cloudwatch").put_metric_data(
        Namespace="fraud-lake/pipeline",
        MetricData=[
            {"MetricName": "GoldTransactionsAggregated", "Value": transactions, "Unit": "Count"},
            {
                "MetricName": "GoldFraudRatePct",
                "Value": (100.0 * fraud / transactions) if transactions else 0.0,
                "Unit": "Percent",
            },
            {
                "MetricName": "GoldFraudLossUsd",
                "Value": float(summary["loss"] or 0.0),
                "Unit": "None",
            },
        ],
    )


if __name__ == "__main__":
    main()
