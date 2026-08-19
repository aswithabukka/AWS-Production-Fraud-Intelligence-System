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
from ml.feedback import apply_feedback
from ml.value import fraud_value_daily

REQUIRED_ARGS = [
    "JOB_NAME",
    "warehouse_uri",
    "silver_table",
    "scores_table",
    "metrics_table",
    "value_table",
    "feedback_path",
    "models_path",
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
    value_table = f"{CATALOG}.{args['value_table']}"

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

    # ------------------------------------------------------- label feedback loop
    # Confirmed ground truth (chargeback outcomes, analyst decisions) lands under
    # feedback/ as JSON lines. Every retrain folds it in, so the models track truth
    # as it arrives rather than freezing at the first labels they saw.
    feedback_pdf = None
    try:
        feedback_pdf = spark.read.json(args["feedback_path"].rstrip("/") + "/").toPandas()
    except Exception:  # noqa: BLE001 - an empty/absent feedback prefix is the normal cold state
        logger.info("no feedback found at %s — training on pipeline labels only", args["feedback_path"])
    pdf, fb_stats = apply_feedback(pdf, feedback_pdf)
    if fb_stats["feedback_rows"]:
        logger.info(
            "feedback applied: %s confirmations, %s labels changed by ground truth",
            fb_stats["labels_confirmed"],
            fb_stats["labels_changed"],
        )

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
    metrics["feedback_labels_confirmed"] = fb_stats["labels_confirmed"]
    metrics["feedback_labels_changed"] = fb_stats["labels_changed"]
    metrics["fraud_rate_in_training_pct"] = round(100.0 * float(pdf["is_fraud"].mean()), 4)
    metrics_df = spark.createDataFrame(metrics)

    create_iceberg_table(spark, metrics_table, metrics_df, partition_by=None)
    ensure_table_columns(spark, metrics_table, metrics_df)
    # Append, not merge: every training run's metrics are history worth keeping —
    # model performance over time is itself a dashboard-worthy series.
    metrics_df.writeTo(metrics_table).append()
    logger.info("appended %s metric rows to %s", len(metrics), metrics_table)

    # ------------------------------------------------------------ value table
    # The dollars ledger for the business dashboard: confusion-matrix cells priced per
    # day. MERGE on dt — a retrain re-prices history rather than duplicating it.
    value = fraud_value_daily(pdf, result.scores)
    value["model_run_id"] = run_stamp
    value["computed_at"] = trained_at
    value_df = spark.createDataFrame(value)

    create_iceberg_table(spark, value_table, value_df, partition_by=None)
    ensure_table_columns(spark, value_table, value_df)
    merge_into(spark, value_table, value_df, key_columns=["dt"], temp_view="_value_src")
    logger.info("merged %s daily value rows into %s", len(value), value_table)

    for row in result.metrics.itertuples():
        logger.info(
            "holdout %s: auc=%.4f precision=%.4f recall=%.4f f1=%.4f",
            row.model_name,
            row.holdout_roc_auc,
            row.holdout_precision,
            row.holdout_recall,
            row.holdout_f1,
        )

    _persist_models(result, args["models_path"], run_stamp)
    _emit_metrics(result)
    job.commit()


def _persist_models(result, models_path: str, run_stamp: str) -> None:
    """Serialise the fitted estimators to S3.

    This is the seam for near-real-time scoring later: a Lambda or the API can load
    latest/ and score a single event in milliseconds without retraining anything.
    """
    import json
    import tempfile
    from urllib.parse import urlparse

    import boto3
    import joblib

    parsed = urlparse(models_path.rstrip("/"))
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")
    s3 = boto3.client("s3")

    manifest = {
        "model_run_id": run_stamp,
        "threshold": result.threshold,
        "feature_names": result.feature_names,
        "models": {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        for name, model in result.fitted_models.items():
            local = f"{tmp}/{name}.joblib"
            joblib.dump(model, local)
            key = f"{prefix}/{run_stamp}/{name}.joblib"
            s3.upload_file(local, bucket, key)
            manifest["models"][name] = key
    s3.put_object(
        Bucket=bucket,
        Key=f"{prefix}/latest.json",
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
    )
    logger.info("persisted %s models + manifest to %s", len(manifest["models"]), models_path)


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
