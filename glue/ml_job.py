"""ML job — silver features to ensemble fraud scores in gold.

    silver.transactions (31-day lookback)
      -> pandas (this is a portfolio-scale dataset; the driver holds it comfortably)
      -> ml.ensemble.train_and_score: LightGBM + XGBoost + RandomForest + SVM
         + IsolationForest, equal-weight ensemble, train-split-chosen threshold
      -> gold.transaction_risk_scores  (MERGE on transaction_id — idempotent re-runs)
      -> gold.model_metrics            (append — one row per model per training run)

Runs as a Glue 5 Spark job so the read/write path stays Iceberg like every other stage;
the actual learning is plain sklearn on the driver. lightgbm/xgboost arrive via
--additional-python-modules (sklearn and pandas ship with Glue 5).

Column names in both output tables are part of the SQL agent's prompt surface —
`lightgbm_fraud_probability` is answerable; `p1` is not.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.conf import SparkConf
from pyspark.context import SparkContext
from pyspark.sql import functions as F

from glue.spark_utils import (
    CATALOG,
    create_iceberg_table,
    ensure_table_columns,
    get_logger,
    iceberg_spark_conf,
    merge_into,
)
from ml.ensemble import train_and_score

REQUIRED_ARGS = [
    "JOB_NAME",
    "warehouse_uri",
    "silver_table",
    "scores_table",
    "metrics_table",
]

LOOKBACK_DAYS = 31

logger = get_logger("ml")


def main() -> None:
    args = getResolvedOptions(sys.argv, REQUIRED_ARGS)

    conf = SparkConf()
    for key, value in iceberg_spark_conf(args["warehouse_uri"]).items():
        conf.set(key, value)

    sc = SparkContext.getOrCreate(conf)
    glue_context = GlueContext(sc)
    spark = glue_context.spark_session

    job = Job(glue_context)
    job.init(args["JOB_NAME"], args)

    silver_table = f"{CATALOG}.{args['silver_table']}"
    scores_table = f"{CATALOG}.{args['scores_table']}"
    metrics_table = f"{CATALOG}.{args['metrics_table']}"

    cutoff = (datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)).date()
    silver = spark.table(silver_table).filter(F.col("dt") >= F.lit(cutoff))

    # Only the model's inputs cross into pandas — not the whole silver row — to keep the
    # driver copy as small as the feature set allows.
    needed = [
        "transaction_id",
        "dt",
        "amount",
        "txn_count_1h",
        "txn_count_24h",
        "amount_sum_1h",
        "amount_sum_24h",
        "distinct_merchants_24h",
        "amount_zscore_30d",
        "prior_txn_count_30d",
        "merchant_risk_score",
        "geo_distance_from_prior_km",
        "implied_speed_kmh",
        "seconds_since_prior_txn",
        "fraud_signal_count",
        "device_change_flag",
        "is_high_velocity",
        "is_amount_outlier",
        "is_impossible_travel",
        "channel",
        "is_fraud",
    ]
    pdf = silver.select(*needed).toPandas()
    logger.info("training on %s silver rows (dt >= %s)", len(pdf), cutoff)

    result = train_and_score(pdf)

    trained_at = datetime.now(UTC)
    run_stamp = trained_at.isoformat(timespec="seconds")

    # ------------------------------------------------------------------ scores table
    scores = result.scores.copy()
    scores["scored_at"] = trained_at
    scores["model_run_id"] = run_stamp
    scores_df = spark.createDataFrame(scores)

    create_iceberg_table(spark, scores_table, scores_df, partition_by="dt")
    ensure_table_columns(spark, scores_table, scores_df)
    merge_into(spark, scores_table, scores_df, key_columns=["transaction_id"], temp_view="_scores_src")
    logger.info("merged %s scores into %s", len(scores), scores_table)

    # ----------------------------------------------------------------- metrics table
    metrics = result.metrics.copy()
    metrics["trained_at"] = trained_at
    metrics["model_run_id"] = run_stamp
    metrics["training_rows"] = len(pdf)
    metrics["fraud_rate_in_training_pct"] = round(100.0 * float(pdf["is_fraud"].mean()), 4)
    metrics_df = spark.createDataFrame(metrics)

    create_iceberg_table(spark, metrics_table, metrics_df, partition_by=None)
    ensure_table_columns(spark, metrics_table, metrics_df)
    # Append, not merge: every training run's metrics are history worth keeping —
    # model performance over time is itself a dashboard-worthy series.
    metrics_df.writeTo(metrics_table).append()
    logger.info("appended %s metric rows to %s", len(metrics), metrics_table)

    for row in result.metrics.itertuples():
        logger.info(
            "holdout %s: auc=%.4f precision=%.4f recall=%.4f f1=%.4f",
            row.model_name,
            row.holdout_roc_auc,
            row.holdout_precision,
            row.holdout_recall,
            row.holdout_f1,
        )

    _emit_metrics(result)
    job.commit()


def _emit_metrics(result) -> None:
    import boto3

    by_model = result.metrics.set_index("model_name")
    data = [
        {
            "MetricName": "EnsembleHoldoutAUC",
            "Value": float(by_model.loc["ensemble", "holdout_roc_auc"]),
            "Unit": "None",
        },
        {
            "MetricName": "EnsembleHoldoutF1",
            "Value": float(by_model.loc["ensemble", "holdout_f1"]),
            "Unit": "None",
        },
        {
            "MetricName": "EnsembleDecisionThreshold",
            "Value": float(result.threshold),
            "Unit": "None",
        },
    ]
    boto3.client("cloudwatch").put_metric_data(Namespace="fraud-lake/ml", MetricData=data)


if __name__ == "__main__":
    main()
