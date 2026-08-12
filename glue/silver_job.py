"""Silver job — bronze to enriched fraud features.

    bronze.transactions
      -> per-customer velocity (1h / 24h)
      -> amount z-score vs the customer's trailing 30 days
      -> merchant risk score (SCD merchant dimension)
      -> geo distance and implied speed from the prior transaction
      -> device-change flag
      -> MERGE into silver.transactions (Iceberg, partitioned by dt)

All features are window functions. Nothing is collected to the driver, because the whole
point of doing this in Spark is that it survives the data getting larger.

A lookback window wider than the processing window is read on purpose: computing a 24h
velocity counter from only today's partition would reset every customer's history at
midnight and produce a daily sawtooth in every velocity feature.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F

from glue.spark_utils import CATALOG, create_iceberg_table, get_logger, iceberg_spark_conf, merge_into
from glue.transforms import build_silver, merchant_risk_scores

REQUIRED_ARGS = [
    "JOB_NAME",
    "warehouse_uri",
    "bronze_table",
    "silver_table",
    "merchant_dim_table",
]

# Feature lookback. The widest window in silver is the 30-day amount baseline; anything
# less and the z-score is computed against a truncated history.
LOOKBACK_DAYS = 31

logger = get_logger("silver")


def main() -> None:
    args = getResolvedOptions(sys.argv, REQUIRED_ARGS)

    sc = SparkContext.getOrCreate()
    glue_context = GlueContext(sc)
    spark = glue_context.spark_session
    for key, value in iceberg_spark_conf(args["warehouse_uri"]).items():
        spark.conf.set(key, value)

    job = Job(glue_context)
    job.init(args["JOB_NAME"], args)

    bronze_table = f"{CATALOG}.{args['bronze_table']}"
    silver_table = f"{CATALOG}.{args['silver_table']}"
    merchant_dim_table = f"{CATALOG}.{args['merchant_dim_table']}"

    cutoff = (datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)).date()
    logger.info("reading %s with dt >= %s", bronze_table, cutoff)

    bronze = spark.table(bronze_table).filter(F.col("dt") >= F.lit(cutoff))

    if bronze.rdd.isEmpty():
        logger.warning("no bronze rows in the lookback window — nothing to do")
        job.commit()
        return

    # The merchant dimension is recomputed over the same window and persisted as its own
    # Iceberg table. Keeping it materialised rather than deriving it inline means the
    # gold layer and the agent can both read the same risk scores the features were
    # built from — a score that only exists inside one job's DAG cannot be audited.
    merchant_dim = merchant_risk_scores(bronze).withColumn("dim_computed_at", F.current_timestamp())
    create_iceberg_table(spark, merchant_dim_table, merchant_dim, partition_by=None)
    merge_into(spark, merchant_dim_table, merchant_dim, key_columns=["merchant_id"], temp_view="_dim_src")

    silver = build_silver(bronze, merchant_dim.drop("dim_computed_at")).cache()
    silver_count = silver.count()
    logger.info("silver rows computed: %s", silver_count)

    if silver_count:
        create_iceberg_table(spark, silver_table, silver, partition_by="dt")
        merge_into(spark, silver_table, silver, key_columns=["transaction_id"])

    _log_feature_health(silver)
    _emit_metrics(silver, silver_count)
    job.commit()


def _log_feature_health(silver) -> None:
    """Log how often each fraud signal fires.

    A feature that fires on 0% or 100% of rows is broken, and finding that out from the
    job log beats finding it out from a model that learned nothing. This is also the
    evidence that the generator's injected anomalies are actually being caught.
    """
    stats = silver.agg(
        F.avg(F.col("is_high_velocity").cast("double")).alias("high_velocity_rate"),
        F.avg(F.col("is_amount_outlier").cast("double")).alias("amount_outlier_rate"),
        F.avg(F.col("is_impossible_travel").cast("double")).alias("impossible_travel_rate"),
        F.avg(F.col("device_change_flag").cast("double")).alias("device_change_rate"),
        F.avg(F.col("is_fraud").cast("double")).alias("fraud_rate"),
    ).collect()[0]

    for name in stats.asDict():
        value = stats[name]
        logger.info("feature health: %s = %.4f", name, value if value is not None else -1.0)


def _emit_metrics(silver, row_count: int) -> None:
    import boto3

    # Recall of the composite signal against the injected ground truth. Only meaningful
    # on synthetic data — it is here because being able to say "the features catch 90% of
    # what I injected" is a far stronger interview answer than "the features compute".
    caught = silver.filter(F.col("is_fraud") & (F.col("fraud_signal_count") > 0)).count()
    total_fraud = silver.filter(F.col("is_fraud")).count()
    recall = (100.0 * caught / total_fraud) if total_fraud else 0.0

    boto3.client("cloudwatch").put_metric_data(
        Namespace="fraud-lake/pipeline",
        MetricData=[
            {"MetricName": "SilverRowsWritten", "Value": float(row_count), "Unit": "Count"},
            {"MetricName": "FraudSignalRecallPct", "Value": recall, "Unit": "Percent"},
        ],
    )
    logger.info("silver rows=%s fraud signal recall=%.2f%%", row_count, recall)


if __name__ == "__main__":
    main()
