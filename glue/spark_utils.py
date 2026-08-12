"""Glue/Spark plumbing shared by the three job entry points.

Kept out of `transforms.py` on purpose: everything here touches Glue or Iceberg, and
`transforms.py` must stay importable (and testable) without either.
"""

from __future__ import annotations

import logging
import sys

from pyspark.sql import DataFrame, SparkSession

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s :: %(message)s"

CATALOG = "glue_catalog"


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout, force=True)
    return logging.getLogger(name)


def iceberg_spark_conf(warehouse_uri: str) -> dict[str, str]:
    """Spark configuration binding Iceberg to the Glue Data Catalog.

    The job must also be started with `--datalake-formats iceberg` so Glue puts the
    Iceberg runtime on the classpath — setting these properties without that flag is the
    most common "ClassNotFoundException: SparkCatalog" on Glue.
    """
    return {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        f"spark.sql.catalog.{CATALOG}": "org.apache.iceberg.spark.SparkCatalog",
        f"spark.sql.catalog.{CATALOG}.warehouse": warehouse_uri,
        f"spark.sql.catalog.{CATALOG}.catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
        f"spark.sql.catalog.{CATALOG}.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
        f"spark.sql.catalog.{CATALOG}.glue.skip-name-validation": "false",
        # Iceberg's own conflict retry. Two overlapping runs should serialise, not
        # clobber each other's snapshot.
        f"spark.sql.catalog.{CATALOG}.commit.retry.num-retries": "4",
        # Small-file control: the raw zone produces many small Firehose objects and
        # without this the bronze table inherits that shape.
        "spark.sql.shuffle.partitions": "8",
        "spark.sql.adaptive.enabled": "true",
        "spark.sql.adaptive.coalescePartitions.enabled": "true",
    }


def table_exists(spark: SparkSession, table: str) -> bool:
    try:
        spark.sql(f"DESCRIBE TABLE {table}")
        return True
    except Exception:  # noqa: BLE001 - Spark raises several unrelated types here
        return False


def create_iceberg_table(
    spark: SparkSession,
    table: str,
    df: DataFrame,
    partition_by: str | None = "dt",
    sort_by: str | None = None,
) -> None:
    """Create the Iceberg table from a DataFrame's schema if it does not exist yet.

    Uses the DataFrameWriterV2 API rather than string DDL so the schema comes from the
    DataFrame and cannot drift from what the job actually writes.
    """
    if table_exists(spark, table):
        return

    writer = df.limit(0).writeTo(table).using("iceberg")
    if partition_by:
        writer = writer.partitionedBy(df[partition_by])
    writer = writer.tableProperty("format-version", "2").tableProperty(
        "write.parquet.compression-codec", "zstd"
    )
    if sort_by:
        writer = writer.tableProperty("sort-order", sort_by)
    writer.create()


def merge_into(
    spark: SparkSession,
    target_table: str,
    source: DataFrame,
    key_columns: list[str],
    temp_view: str = "_merge_source",
) -> None:
    """Idempotent upsert into an Iceberg table.

    MERGE rather than append is what makes a re-run safe. Firehose gives at-least-once
    delivery and Step Functions retries on failure, so the same batch WILL be processed
    twice at some point; with an append the row count silently doubles.
    """
    source.createOrReplaceTempView(temp_view)
    condition = " AND ".join(f"t.{col} = s.{col}" for col in key_columns)
    spark.sql(f"""
        MERGE INTO {target_table} t
        USING {temp_view} s
        ON {condition}
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)


def overwrite_partitions(spark: SparkSession, target_table: str, source: DataFrame) -> None:
    """Replace only the partitions present in `source`, leaving the rest untouched.

    The right semantics for the gold layer: a daily aggregate is a full recomputation of
    its own day, and merging individual aggregate rows would be wrong if the underlying
    grain changed.
    """
    source.writeTo(target_table).overwritePartitions()


def expire_old_snapshots(spark: SparkSession, table: str, retain_days: int = 7) -> None:
    """Drop Iceberg snapshots older than `retain_days`.

    COST: every snapshot pins the data files it references, so without expiry the S3
    footprint grows monotonically no matter how much data is logically deleted. Seven days
    still leaves plenty of history for the time-travel demo.
    """
    spark.sql(f"""
        CALL {CATALOG}.system.expire_snapshots(
            table => '{table.split(".", 1)[1] if table.startswith(CATALOG) else table}',
            older_than => TIMESTAMP '{_days_ago_literal(retain_days)}',
            retain_last => 5
        )
    """)


def _days_ago_literal(days: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
