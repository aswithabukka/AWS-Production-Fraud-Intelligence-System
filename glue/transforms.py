"""Pure PySpark transforms for the bronze, silver, and gold layers.

Nothing in this module imports `awsglue` or touches AWS. Every function takes DataFrames
and returns DataFrames, which is what makes the whole transformation layer testable with
`pytest` against small fixtures instead of a 10-minute Glue run. The job entry points in
`bronze_job.py` / `silver_job.py` / `gold_job.py` are thin wrappers that read, call these,
and write Iceberg.

Design rule throughout: window functions, never collect-to-driver. A `collect()` in a
feature pipeline is a correctness bug waiting for the data to grow.
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

# ---------------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------------

# Raw records land as JSON with every field a string (Firehose does no conversion, and
# a corrupted record can put "N/A" where a number belongs). Bronze is the first place
# types are enforced.
BRONZE_SCHEMA = T.StructType(
    [
        T.StructField("transaction_id", T.StringType(), False),
        T.StructField("customer_id", T.StringType(), False),
        T.StructField("merchant_id", T.StringType(), False),
        T.StructField("mcc", T.StringType(), True),
        T.StructField("amount", T.DecimalType(12, 2), True),
        T.StructField("currency", T.StringType(), True),
        T.StructField("transaction_timestamp", T.TimestampType(), False),
        T.StructField("lat", T.DoubleType(), True),
        T.StructField("lon", T.DoubleType(), True),
        T.StructField("device_id", T.StringType(), True),
        T.StructField("channel", T.StringType(), True),
        T.StructField("is_fraud", T.BooleanType(), True),
        T.StructField("auth_response_code", T.StringType(), True),
        T.StructField("ingest_timestamp", T.TimestampType(), True),
        T.StructField("schema_version", T.IntegerType(), True),
        T.StructField("anomaly_type", T.StringType(), True),
        T.StructField("dt", T.DateType(), False),
    ]
)

BRONZE_COLUMNS = [f.name for f in BRONZE_SCHEMA.fields]

# Bounds used by both the bronze validator and the Glue Data Quality ruleset. Defined
# once here so the two can never disagree — a DQ rule that contradicts the ingest
# validator produces a permanently failing gate that nobody can explain.
MIN_AMOUNT = 0.01
MAX_AMOUNT = 25_000.00
VALID_CHANNELS = ("card_present", "ecommerce", "contactless", "recurring")

EARTH_RADIUS_KM = 6371.0


# ---------------------------------------------------------------------------------
# Bronze
# ---------------------------------------------------------------------------------


def _parse_timestamp(col: Column) -> Column:
    """Producer emits ISO-8601 with milliseconds and an offset. `to_timestamp` without a
    format handles that, and returns NULL rather than raising on garbage — which is what
    lets a bad timestamp become a quarantine row instead of a failed job."""
    return F.to_timestamp(col)


def _rejection_reasons(df: DataFrame) -> Column:
    """Build an array of every rule a record violates.

    Deliberately *all* reasons, not the first one. A record that is both missing a
    customer_id and carrying a negative amount is more interesting than one with a single
    defect, and collapsing to the first failure destroys that signal in the quarantine
    table.
    """
    checks: list[tuple[str, Column]] = [
        ("null_transaction_id", F.col("transaction_id").isNull() | (F.trim(F.col("transaction_id")) == "")),
        ("null_customer_id", F.col("customer_id").isNull() | (F.trim(F.col("customer_id")) == "")),
        ("null_merchant_id", F.col("merchant_id").isNull() | (F.trim(F.col("merchant_id")) == "")),
        # cast() returns NULL on a non-numeric string, so this catches "N/A" as well as
        # a genuinely absent amount.
        ("non_numeric_amount", F.col("amount").isNotNull() & F.col("amount").cast("double").isNull()),
        ("missing_amount", F.col("amount").isNull()),
        (
            "amount_out_of_range",
            F.col("amount").cast("double").isNotNull()
            & (
                (F.col("amount").cast("double") < F.lit(MIN_AMOUNT))
                | (F.col("amount").cast("double") > F.lit(MAX_AMOUNT))
            ),
        ),
        ("unparseable_timestamp", _parse_timestamp(F.col("timestamp")).isNull()),
        (
            "lat_out_of_range",
            F.col("lat").cast("double").isNotNull()
            & ((F.col("lat").cast("double") < -90) | (F.col("lat").cast("double") > 90)),
        ),
        (
            "lon_out_of_range",
            F.col("lon").cast("double").isNotNull()
            & ((F.col("lon").cast("double") < -180) | (F.col("lon").cast("double") > 180)),
        ),
        (
            "unknown_channel",
            F.col("channel").isNotNull() & ~F.col("channel").isin(*VALID_CHANNELS),
        ),
    ]

    # array_compact drops the NULLs left by the non-matching branches, so a clean record
    # ends up with an empty array rather than an array of nulls.
    return F.array_compact(F.array(*[F.when(cond, F.lit(name)) for name, cond in checks]))


def split_valid_and_quarantine(raw: DataFrame) -> tuple[DataFrame, DataFrame]:
    """Split raw records into (typed valid rows, quarantine rows).

    Records failing schema or null-critical checks go to quarantine with the reasons
    attached and do NOT enter bronze. This is the halt-the-bad-data boundary — everything
    downstream may assume bronze is well-typed.
    """
    # Every raw column may be missing entirely from the JSON (a dropped key, or a v1
    # record read against the v2 schema), so fill in anything absent as NULL first.
    raw = _ensure_raw_columns(raw)

    flagged = raw.withColumn("rejection_reasons", _rejection_reasons(raw))

    quarantine = (
        flagged.filter(F.size("rejection_reasons") > 0)
        .withColumn("rejection_reason", F.concat_ws(",", F.col("rejection_reasons")))
        .withColumn("rejected_at", F.current_timestamp())
        .withColumn(
            "dt",
            F.coalesce(
                F.to_date(_parse_timestamp(F.col("timestamp"))),
                F.to_date(_parse_timestamp(F.col("ingest_timestamp"))),
                F.current_date(),
            ),
        )
    )

    valid = flagged.filter(F.size("rejection_reasons") == 0).select(
        F.col("transaction_id").cast("string").alias("transaction_id"),
        F.col("customer_id").cast("string").alias("customer_id"),
        F.col("merchant_id").cast("string").alias("merchant_id"),
        F.col("mcc").cast("string").alias("mcc"),
        F.col("amount").cast(T.DecimalType(12, 2)).alias("amount"),
        F.coalesce(F.col("currency"), F.lit("USD")).cast("string").alias("currency"),
        _parse_timestamp(F.col("timestamp")).alias("transaction_timestamp"),
        F.col("lat").cast("double").alias("lat"),
        F.col("lon").cast("double").alias("lon"),
        F.col("device_id").cast("string").alias("device_id"),
        F.col("channel").cast("string").alias("channel"),
        # The producer emits a JSON boolean; the raw table reads it back as the string
        # "true"/"false". cast("boolean") handles both.
        F.col("is_fraud").cast("boolean").alias("is_fraud"),
        F.col("auth_response_code").cast("string").alias("auth_response_code"),
        F.coalesce(_parse_timestamp(F.col("ingest_timestamp")), F.current_timestamp()).alias(
            "ingest_timestamp"
        ),
        F.coalesce(F.col("schema_version").cast("int"), F.lit(1)).alias("schema_version"),
        F.col("anomaly_type").cast("string").alias("anomaly_type"),
        F.to_date(_parse_timestamp(F.col("timestamp"))).alias("dt"),
    )

    return valid, quarantine


def _ensure_raw_columns(raw: DataFrame) -> DataFrame:
    """Add any expected raw column that is absent as a typed NULL.

    This is what makes the pipeline forward- and backward-compatible: a v1 record has no
    `auth_response_code` and a v2 record does, and the same bronze job must handle both
    without a branch.
    """
    expected = [
        "transaction_id",
        "customer_id",
        "merchant_id",
        "mcc",
        "amount",
        "currency",
        "timestamp",
        "lat",
        "lon",
        "device_id",
        "channel",
        "is_fraud",
        "ingest_timestamp",
        "schema_version",
        "anomaly_type",
        "auth_response_code",
    ]
    missing = [c for c in expected if c not in raw.columns]
    for column in missing:
        raw = raw.withColumn(column, F.lit(None).cast("string"))
    return raw


def dedupe_transactions(df: DataFrame) -> DataFrame:
    """Keep one row per transaction_id — the latest by ingest_timestamp.

    At-least-once delivery is the contract Kinesis and Firehose actually give you, so
    duplicates are normal operation, not an incident. Ties are broken deterministically
    on ingest_timestamp then transaction hash, so a replay of the identical batch cannot
    change which row survives.
    """
    ordering = Window.partitionBy("transaction_id").orderBy(
        F.col("ingest_timestamp").desc_nulls_last(),
        F.col("transaction_timestamp").desc_nulls_last(),
    )
    return (
        df.withColumn("_row_number", F.row_number().over(ordering))
        .filter(F.col("_row_number") == 1)
        .drop("_row_number")
    )


def build_bronze(raw: DataFrame) -> tuple[DataFrame, DataFrame]:
    """raw JSON -> (bronze rows, quarantine rows)."""
    valid, quarantine = split_valid_and_quarantine(raw)
    return dedupe_transactions(valid), quarantine


# ---------------------------------------------------------------------------------
# Silver — fraud features
# ---------------------------------------------------------------------------------


def haversine_km(lat1: Column, lon1: Column, lat2: Column, lon2: Column) -> Column:
    """Great-circle distance in km, expressed entirely in Spark SQL.

    A Python UDF here would serialise every row to the Python worker and back; this stays
    in the JVM and vectorises. Mirrors `ingestion.entities.haversine_km` so the injected
    impossible-geography anomalies are measured with the same maths that generated them.
    """
    phi1, phi2 = F.radians(lat1), F.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = F.radians(lon2 - lon1)
    a = F.pow(F.sin(d_phi / 2), 2) + F.cos(phi1) * F.cos(phi2) * F.pow(F.sin(d_lambda / 2), 2)

    # Cap at 1.0 so floating-point drift on near-antipodal points cannot push asin's
    # argument above 1 and produce NaN.
    #
    # The isNotNull() guard is load-bearing: Spark's least() *skips* NULLs rather than
    # propagating them, so least(NULL, 1.0) returns 1.0 — which would silently turn
    # "this customer has no prior transaction" into a 20,015 km antipodal distance and
    # flag every customer's first transaction as impossible travel.
    capped = F.when(a.isNotNull(), F.least(a, F.lit(1.0)))
    return F.lit(2 * EARTH_RADIUS_KM) * F.asin(F.sqrt(capped))


def _seconds(col: Column) -> Column:
    return col.cast("timestamp").cast("long")


def add_velocity_features(df: DataFrame) -> DataFrame:
    """Per-customer transaction counts and sums over trailing 1h and 24h windows.

    Range-based windows over the epoch-second cast, not row-based: "the last 10
    transactions" and "the last hour" are different questions, and card-testing bursts
    are defined in time, not in row count.
    """
    base = Window.partitionBy("customer_id").orderBy(_seconds(F.col("transaction_timestamp")))
    hour = base.rangeBetween(-3600, 0)
    day = base.rangeBetween(-86400, 0)

    return (
        df.withColumn("txn_count_1h", F.count("*").over(hour))
        .withColumn("txn_count_24h", F.count("*").over(day))
        .withColumn("amount_sum_1h", F.sum(F.col("amount").cast("double")).over(hour))
        .withColumn("amount_sum_24h", F.sum(F.col("amount").cast("double")).over(day))
        .withColumn("distinct_merchants_24h", F.size(F.collect_set("merchant_id").over(day)))
    )


def add_amount_zscore(df: DataFrame) -> DataFrame:
    """Amount z-score against the customer's trailing 30 days.

    Excludes the current row from its own baseline — including it drags the mean toward
    the outlier and systematically shrinks the score of exactly the transactions the
    feature exists to catch. With fewer than 3 prior transactions the score is NULL
    rather than 0.0: "no baseline" and "perfectly average" are different states, and
    encoding them identically is a subtle way to lie to a downstream model.
    """
    trailing_30d = (
        Window.partitionBy("customer_id")
        .orderBy(_seconds(F.col("transaction_timestamp")))
        .rangeBetween(-30 * 86400, -1)
    )

    mean_30d = F.avg(F.col("amount").cast("double")).over(trailing_30d)
    std_30d = F.stddev_samp(F.col("amount").cast("double")).over(trailing_30d)
    count_30d = F.count("*").over(trailing_30d)

    return (
        df.withColumn("amount_mean_30d", F.round(mean_30d, 4))
        .withColumn("amount_stddev_30d", F.round(std_30d, 4))
        .withColumn("prior_txn_count_30d", count_30d)
        .withColumn(
            "amount_zscore_30d",
            F.when(
                (count_30d >= 3) & std_30d.isNotNull() & (std_30d > 0),
                F.round((F.col("amount").cast("double") - mean_30d) / std_30d, 4),
            ).otherwise(F.lit(None).cast("double")),
        )
    )


def add_geo_features(df: DataFrame) -> DataFrame:
    """Distance and implied travel speed from the customer's previous transaction.

    Implied speed is the feature that actually discriminates: 6,000 km apart is a flight,
    6,000 km apart in eight minutes is a cloned card.
    """
    by_customer = Window.partitionBy("customer_id").orderBy(F.col("transaction_timestamp"))

    prev_lat = F.lag("lat").over(by_customer)
    prev_lon = F.lag("lon").over(by_customer)
    prev_ts = F.lag("transaction_timestamp").over(by_customer)
    prev_device = F.lag("device_id").over(by_customer)

    distance = haversine_km(prev_lat, prev_lon, F.col("lat"), F.col("lon"))
    elapsed_hours = (_seconds(F.col("transaction_timestamp")) - _seconds(prev_ts)) / 3600.0

    return (
        df.withColumn("seconds_since_prior_txn", _seconds(F.col("transaction_timestamp")) - _seconds(prev_ts))
        .withColumn("geo_distance_from_prior_km", F.round(distance, 3))
        .withColumn(
            "implied_speed_kmh",
            # Same-second transactions would divide by zero; they are also the most
            # suspicious case, so they get a large sentinel rather than NULL.
            F.when(elapsed_hours > 0, F.round(distance / elapsed_hours, 2))
            .when(elapsed_hours.isNotNull() & (distance > 1), F.lit(99999.0))
            .otherwise(F.lit(None).cast("double")),
        )
        .withColumn(
            "device_change_flag",
            F.when(prev_device.isNull(), F.lit(False)).otherwise(prev_device != F.col("device_id")),
        )
        .drop("_prev_lat", "_prev_lon")
    )


def merchant_risk_scores(bronze: DataFrame, min_transactions: int = 20) -> DataFrame:
    """Per-merchant historical fraud rate, smoothed toward the global rate.

    Laplace/additive smoothing with a pseudo-count: a merchant with 1 transaction that
    happened to be fraud is not a 100%-risk merchant, and an unsmoothed rate would say it
    is. `min_transactions` sets how much evidence is needed before the merchant's own rate
    dominates the prior.
    """
    global_rate = bronze.select(F.avg(F.col("is_fraud").cast("double")).alias("r")).collect()[0]["r"] or 0.0

    return (
        bronze.groupBy("merchant_id")
        .agg(
            F.count("*").alias("merchant_txn_count"),
            F.sum(F.col("is_fraud").cast("int")).alias("merchant_fraud_count"),
            F.avg(F.col("amount").cast("double")).alias("merchant_avg_amount"),
        )
        .withColumn(
            "merchant_risk_score",
            F.round(
                (F.col("merchant_fraud_count") + F.lit(global_rate * min_transactions))
                / (F.col("merchant_txn_count") + F.lit(min_transactions)),
                6,
            ),
        )
        .withColumn(
            "merchant_risk_tier",
            F.when(F.col("merchant_risk_score") >= 0.05, F.lit("high"))
            .when(F.col("merchant_risk_score") >= 0.02, F.lit("medium"))
            .otherwise(F.lit("low")),
        )
    )


def build_silver(bronze: DataFrame, merchant_dim: DataFrame | None = None) -> DataFrame:
    """bronze -> silver.transactions with the full fraud feature set.

    `merchant_dim` is the slowly-changing merchant dimension. When omitted it is derived
    from bronze itself, which is what the standalone job does on a cold start.
    """
    dim = merchant_dim if merchant_dim is not None else merchant_risk_scores(bronze)

    featured = add_geo_features(add_amount_zscore(add_velocity_features(bronze)))

    return (
        featured.join(F.broadcast(dim), on="merchant_id", how="left")
        # A merchant absent from the dimension is new, not safe. Defaulting an unknown
        # merchant's risk to 0 would make brand-new mule merchants the lowest-risk rows
        # in the table.
        .withColumn("merchant_risk_score", F.coalesce(F.col("merchant_risk_score"), F.lit(0.02)))
        .withColumn("merchant_risk_tier", F.coalesce(F.col("merchant_risk_tier"), F.lit("unknown")))
        .withColumn(
            "is_high_velocity",
            (F.col("txn_count_1h") >= 5) | (F.col("txn_count_24h") >= 20),
        )
        .withColumn("is_amount_outlier", F.coalesce(F.abs(F.col("amount_zscore_30d")) >= 3.0, F.lit(False)))
        .withColumn("is_impossible_travel", F.coalesce(F.col("implied_speed_kmh") > 900.0, F.lit(False)))
        .withColumn(
            "fraud_signal_count",
            F.col("is_high_velocity").cast("int")
            + F.col("is_amount_outlier").cast("int")
            + F.col("is_impossible_travel").cast("int")
            + F.col("device_change_flag").cast("int"),
        )
        .withColumn("silver_processed_at", F.current_timestamp())
    )


# ---------------------------------------------------------------------------------
# Gold — the tables the SQL agent queries
# ---------------------------------------------------------------------------------
#
# Column names in this layer are part of the agent's prompt surface: the SQL agent
# introspects the Glue Catalog and writes queries from these names alone. `fraud_rate_pct`
# is unambiguous; `rate` would produce wrong SQL eventually. Verbosity here is a feature.


def build_fraud_metrics_daily(silver: DataFrame) -> DataFrame:
    """Daily fraud aggregates by MCC and channel."""
    approved = F.col("auth_response_code").isNull() | (F.col("auth_response_code") == "00")

    return (
        silver.groupBy("dt", "mcc", "channel")
        .agg(
            F.count("*").alias("transaction_count"),
            F.countDistinct("customer_id").alias("distinct_customer_count"),
            F.countDistinct("merchant_id").alias("distinct_merchant_count"),
            F.sum(F.col("is_fraud").cast("int")).alias("fraud_transaction_count"),
            F.round(F.sum(F.col("amount").cast("double")), 2).alias("total_amount_usd"),
            F.round(F.sum(F.when(F.col("is_fraud"), F.col("amount").cast("double")).otherwise(0.0)), 2).alias(
                "fraud_loss_amount_usd"
            ),
            F.round(F.avg(F.col("amount").cast("double")), 2).alias("avg_transaction_amount_usd"),
            F.sum(F.when(approved, 1).otherwise(0)).alias("approved_transaction_count"),
            F.sum(F.col("fraud_signal_count")).alias("total_fraud_signals"),
        )
        .withColumn(
            "fraud_rate_pct",
            F.round(100.0 * F.col("fraud_transaction_count") / F.col("transaction_count"), 4),
        )
        .withColumn(
            "approval_rate_pct",
            F.round(100.0 * F.col("approved_transaction_count") / F.col("transaction_count"), 4),
        )
        .withColumn(
            "fraud_loss_rate_pct",
            # NULL when there was no volume, expressed with when() rather than
            # F.nullif(): Glue 5's Spark build rejects nullif over unresolved columns
            # ("Invalid call to dataType on unresolved object") even though stock
            # PySpark 3.5 accepts it — found on the first live gold run.
            F.round(
                F.when(
                    F.col("total_amount_usd") > 0,
                    100.0 * F.col("fraud_loss_amount_usd") / F.col("total_amount_usd"),
                ),
                4,
            ),
        )
        .withColumn("gold_computed_at", F.current_timestamp())
    )


def build_merchant_risk(silver: DataFrame) -> DataFrame:
    """Per-merchant risk rollup — the table behind "which merchants are riskiest"."""
    return (
        silver.groupBy("merchant_id", "mcc")
        .agg(
            F.count("*").alias("transaction_count"),
            F.sum(F.col("is_fraud").cast("int")).alias("fraud_transaction_count"),
            F.round(F.sum(F.col("amount").cast("double")), 2).alias("total_amount_usd"),
            F.round(F.sum(F.when(F.col("is_fraud"), F.col("amount").cast("double")).otherwise(0.0)), 2).alias(
                "fraud_loss_amount_usd"
            ),
            F.round(F.avg(F.col("amount").cast("double")), 2).alias("avg_transaction_amount_usd"),
            F.countDistinct("customer_id").alias("distinct_customer_count"),
            F.round(F.max("merchant_risk_score"), 6).alias("merchant_risk_score"),
            F.max("merchant_risk_tier").alias("merchant_risk_tier"),
            F.min("dt").alias("first_seen_date"),
            F.max("dt").alias("last_seen_date"),
        )
        .withColumn(
            "fraud_rate_pct",
            F.round(100.0 * F.col("fraud_transaction_count") / F.col("transaction_count"), 4),
        )
        .withColumn("gold_computed_at", F.current_timestamp())
    )
