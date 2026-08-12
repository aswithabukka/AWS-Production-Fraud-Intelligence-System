"""Tests for the bronze/silver/gold transforms.

Every test builds a handful of rows by hand so the expected answer can be worked out on
paper. That is the point: if a velocity counter is wrong, this file says which one and
why, in under a second, without a Glue run.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("pyspark")

from pyspark.sql import functions as F  # noqa: E402

from glue.transforms import (  # noqa: E402
    add_amount_zscore,
    add_geo_features,
    add_velocity_features,
    build_bronze,
    build_fraud_metrics_daily,
    build_merchant_risk,
    build_silver,
    dedupe_transactions,
    haversine_km,
    merchant_risk_scores,
    split_valid_and_quarantine,
)

BASE = datetime(2026, 3, 1, 12, 0, 0)

RAW_FIELDS = [
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
]


def raw_row(**overrides) -> dict:
    """A clean raw record. Every field is a string, as it arrives from the JSON landing
    zone — that is what bronze has to cope with."""
    row = {
        "transaction_id": "T1",
        "customer_id": "CUST1",
        "merchant_id": "MER1",
        "mcc": "5411",
        "amount": "42.50",
        "currency": "USD",
        "timestamp": BASE.isoformat(),
        "lat": "40.7128",
        "lon": "-74.0060",
        "device_id": "DEV1",
        "channel": "ecommerce",
        "is_fraud": "false",
        "ingest_timestamp": BASE.isoformat(),
        "schema_version": "1",
        "anomaly_type": None,
    }
    row.update(overrides)
    return row


def string_schema(fields: list[str]):
    """Explicit all-string schema. Type inference is not an option here: a fixture column
    that is None in every row has no inferable type, and more importantly the raw zone
    genuinely is all-strings — inferring would test a shape production never sees."""
    from pyspark.sql import types as T

    return T.StructType([T.StructField(name, T.StringType(), True) for name in fields])


def raw_df(spark, rows: list[dict], fields: list[str] | None = None):
    fields = fields or RAW_FIELDS
    return spark.createDataFrame([[r.get(f) for f in fields] for r in rows], schema=string_schema(fields))


# ---------------------------------------------------------------------------- bronze


def test_clean_records_pass_through_to_bronze(spark):
    valid, quarantine = split_valid_and_quarantine(raw_df(spark, [raw_row()]))
    assert valid.count() == 1
    assert quarantine.count() == 0

    row = valid.collect()[0]
    assert row["transaction_id"] == "T1"
    assert float(row["amount"]) == 42.50
    assert row["is_fraud"] is False
    assert row["transaction_timestamp"] == BASE
    assert str(row["dt"]) == "2026-03-01"


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"transaction_id": None}, "null_transaction_id"),
        ({"transaction_id": "  "}, "null_transaction_id"),
        ({"customer_id": None}, "null_customer_id"),
        ({"merchant_id": None}, "null_merchant_id"),
        ({"amount": "N/A"}, "non_numeric_amount"),
        ({"amount": None}, "missing_amount"),
        ({"amount": "-19.99"}, "amount_out_of_range"),
        ({"amount": "999999.00"}, "amount_out_of_range"),
        ({"timestamp": "not-a-timestamp"}, "unparseable_timestamp"),
        ({"lat": "181.5"}, "lat_out_of_range"),
        ({"lon": "-420.0"}, "lon_out_of_range"),
        ({"channel": "carrier_pigeon"}, "unknown_channel"),
    ],
)
def test_each_defect_is_quarantined_with_its_reason(spark, overrides, expected_reason):
    valid, quarantine = split_valid_and_quarantine(raw_df(spark, [raw_row(**overrides)]))

    assert valid.count() == 0, f"{overrides} should not reach bronze"
    assert quarantine.count() == 1
    assert expected_reason in quarantine.collect()[0]["rejection_reason"]


def test_a_record_reports_every_reason_it_violates(spark):
    """Collapsing to the first failure would hide that this record is doubly broken."""
    bad = raw_row(customer_id=None, amount="-5.00")
    _, quarantine = split_valid_and_quarantine(raw_df(spark, [bad]))

    reasons = quarantine.collect()[0]["rejection_reason"]
    assert "null_customer_id" in reasons
    assert "amount_out_of_range" in reasons


def test_quarantine_rows_still_get_a_partition_date(spark):
    """A record with an unparseable timestamp still has to land somewhere — falling back
    to the ingest time keeps it queryable instead of failing the write."""
    _, quarantine = split_valid_and_quarantine(raw_df(spark, [raw_row(timestamp="garbage")]))
    assert quarantine.collect()[0]["dt"] is not None


def test_valid_and_quarantine_partition_the_input(spark):
    """No record may be silently dropped: valid + quarantine must equal the input."""
    rows = [
        raw_row(transaction_id="T1"),
        raw_row(transaction_id="T2", amount="N/A"),
        raw_row(transaction_id="T3", channel="pigeon"),
        raw_row(transaction_id="T4"),
    ]
    valid, quarantine = split_valid_and_quarantine(raw_df(spark, rows))
    assert valid.count() + quarantine.count() == len(rows)


def test_dedupe_keeps_the_latest_by_ingest_timestamp(spark):
    """At-least-once delivery means duplicates are normal traffic, not an incident."""
    rows = [
        raw_row(transaction_id="T1", amount="10.00", ingest_timestamp=BASE.isoformat()),
        raw_row(
            transaction_id="T1",
            amount="99.00",
            ingest_timestamp=(BASE + timedelta(minutes=5)).isoformat(),
        ),
        raw_row(transaction_id="T2", amount="20.00"),
    ]
    valid, _ = split_valid_and_quarantine(raw_df(spark, rows))
    deduped = dedupe_transactions(valid)

    assert deduped.count() == 2
    kept = {r["transaction_id"]: float(r["amount"]) for r in deduped.collect()}
    assert kept["T1"] == 99.00, "the later ingest should win"


def test_replaying_an_identical_batch_does_not_change_the_row_count(spark):
    """The duplicate-replay failure scenario, as a unit test."""
    rows = [raw_row(transaction_id=f"T{i}") for i in range(5)]

    once, _ = build_bronze(raw_df(spark, rows))
    twice, _ = build_bronze(raw_df(spark, rows + rows))

    assert once.count() == twice.count() == 5


def test_v1_records_survive_the_v2_schema(spark):
    """Schema evolution: a record with no auth_response_code must still load, with the
    column present and NULL. Iceberg only supports additive evolution, so this is the
    property the whole demo rests on."""
    valid, _ = split_valid_and_quarantine(raw_df(spark, [raw_row()]))
    assert "auth_response_code" in valid.columns
    assert valid.collect()[0]["auth_response_code"] is None


def test_v2_records_carry_the_new_field(spark):
    fields = [*RAW_FIELDS, "auth_response_code"]
    row = raw_row(schema_version="2")
    row["auth_response_code"] = "05"
    df = spark.createDataFrame([[row.get(f) for f in fields]], schema=string_schema(fields))

    valid, _ = split_valid_and_quarantine(df)
    assert valid.collect()[0]["auth_response_code"] == "05"


# ---------------------------------------------------------------------------- silver


def bronze_df(spark, rows: list[dict]):
    """Build a bronze-shaped DataFrame straight from dicts, bypassing the raw layer."""
    valid, _ = split_valid_and_quarantine(raw_df(spark, rows))
    return valid


def test_velocity_counts_are_time_windowed_not_row_windowed(spark):
    """Three transactions inside an hour, one 90 minutes later. The late one must see
    itself plus the earlier three in 24h, but only itself in 1h."""
    rows = [
        raw_row(transaction_id="T1", timestamp=BASE.isoformat()),
        raw_row(transaction_id="T2", timestamp=(BASE + timedelta(minutes=10)).isoformat()),
        raw_row(transaction_id="T3", timestamp=(BASE + timedelta(minutes=20)).isoformat()),
        raw_row(transaction_id="T4", timestamp=(BASE + timedelta(minutes=110)).isoformat()),
    ]
    result = add_velocity_features(bronze_df(spark, rows))
    by_id = {r["transaction_id"]: r for r in result.collect()}

    assert by_id["T1"]["txn_count_1h"] == 1
    assert by_id["T3"]["txn_count_1h"] == 3
    assert by_id["T4"]["txn_count_1h"] == 1, "the 90-minute gap must fall outside the 1h window"
    assert by_id["T4"]["txn_count_24h"] == 4


def test_velocity_is_scoped_per_customer(spark):
    rows = [
        raw_row(transaction_id="T1", customer_id="A"),
        raw_row(transaction_id="T2", customer_id="A", timestamp=(BASE + timedelta(minutes=1)).isoformat()),
        raw_row(transaction_id="T3", customer_id="B", timestamp=(BASE + timedelta(minutes=2)).isoformat()),
    ]
    result = add_velocity_features(bronze_df(spark, rows))
    by_id = {r["transaction_id"]: r for r in result.collect()}

    assert by_id["T2"]["txn_count_1h"] == 2
    assert by_id["T3"]["txn_count_1h"] == 1, "customer B must not see customer A's activity"


def test_zscore_excludes_the_current_row_from_its_own_baseline(spark):
    """Four ~$10 transactions then one $500. Including the outlier in its own mean would
    drag the baseline up and shrink exactly the score the feature exists to produce."""
    amounts = ["10.00", "12.00", "11.00", "9.00", "500.00"]
    rows = [
        raw_row(transaction_id=f"T{i}", amount=a, timestamp=(BASE + timedelta(hours=i)).isoformat())
        for i, a in enumerate(amounts)
    ]
    result = add_amount_zscore(bronze_df(spark, rows))
    by_id = {r["transaction_id"]: r for r in result.collect()}

    outlier = by_id["T4"]
    assert outlier["prior_txn_count_30d"] == 4
    assert outlier["amount_mean_30d"] == pytest.approx(10.5, abs=0.01)
    assert outlier["amount_zscore_30d"] > 100, "a 500 against a ~10.5 +/- 1.3 baseline is enormous"


def test_zscore_is_null_without_enough_history(spark):
    """'No baseline' and 'perfectly average' are different states. Encoding both as 0.0
    would quietly lie to anything downstream."""
    rows = [
        raw_row(transaction_id="T0", timestamp=BASE.isoformat()),
        raw_row(transaction_id="T1", timestamp=(BASE + timedelta(hours=1)).isoformat()),
    ]
    result = add_amount_zscore(bronze_df(spark, rows))
    assert all(r["amount_zscore_30d"] is None for r in result.collect())


def test_haversine_matches_a_known_distance(spark):
    """New York to Los Angeles is ~3,936 km. Same formula as the producer's, so the
    injected impossible-geography anomalies are measured with the maths that made them."""
    df = spark.createDataFrame([(40.7128, -74.0060, 34.0522, -118.2437)], ["lat1", "lon1", "lat2", "lon2"])
    distance = df.select(
        haversine_km(F.col("lat1"), F.col("lon1"), F.col("lat2"), F.col("lon2")).alias("km")
    ).collect()[0]["km"]

    assert distance == pytest.approx(3936, abs=25)


def test_haversine_is_zero_for_identical_points(spark):
    df = spark.createDataFrame([(40.0, -74.0)], ["lat", "lon"])
    distance = df.select(
        haversine_km(F.col("lat"), F.col("lon"), F.col("lat"), F.col("lon")).alias("km")
    ).collect()[0]["km"]
    assert distance == pytest.approx(0.0, abs=1e-6)


def test_implied_speed_flags_impossible_travel(spark):
    """NYC then Moscow eight minutes later. The distance alone is a flight; the distance
    over the elapsed time is a cloned card."""
    rows = [
        raw_row(transaction_id="T1", lat="40.7128", lon="-74.0060", timestamp=BASE.isoformat()),
        raw_row(
            transaction_id="T2",
            lat="55.7558",
            lon="37.6173",
            timestamp=(BASE + timedelta(minutes=8)).isoformat(),
        ),
    ]
    result = add_geo_features(bronze_df(spark, rows))
    by_id = {r["transaction_id"]: r for r in result.collect()}

    assert by_id["T1"]["geo_distance_from_prior_km"] is None, "no prior transaction"
    assert by_id["T2"]["geo_distance_from_prior_km"] > 7_000
    assert by_id["T2"]["implied_speed_kmh"] > 50_000


def test_normal_travel_is_not_flagged(spark):
    """The same journey with a realistic gap must not look like fraud."""
    rows = [
        raw_row(transaction_id="T1", lat="40.7128", lon="-74.0060", timestamp=BASE.isoformat()),
        raw_row(
            transaction_id="T2",
            lat="34.0522",
            lon="-118.2437",
            timestamp=(BASE + timedelta(hours=7)).isoformat(),
        ),
    ]
    result = add_geo_features(bronze_df(spark, rows))
    speed = {r["transaction_id"]: r["implied_speed_kmh"] for r in result.collect()}["T2"]
    assert speed < 900, "a 7-hour coast-to-coast flight is normal travel"


def test_device_change_flag(spark):
    rows = [
        raw_row(transaction_id="T1", device_id="DEV1"),
        raw_row(transaction_id="T2", device_id="DEV1", timestamp=(BASE + timedelta(hours=1)).isoformat()),
        raw_row(transaction_id="T3", device_id="DEV2", timestamp=(BASE + timedelta(hours=2)).isoformat()),
    ]
    result = add_geo_features(bronze_df(spark, rows))
    by_id = {r["transaction_id"]: r for r in result.collect()}

    assert by_id["T1"]["device_change_flag"] is False, "the first transaction has nothing to change from"
    assert by_id["T2"]["device_change_flag"] is False
    assert by_id["T3"]["device_change_flag"] is True


def test_merchant_risk_is_smoothed_toward_the_global_rate(spark):
    """One merchant, one transaction, which happened to be fraud. An unsmoothed rate
    would call that a 100%-risk merchant on a single observation."""
    rows = [raw_row(transaction_id="T1", merchant_id="MER_NEW", is_fraud="true")]
    rows += [raw_row(transaction_id=f"T{i}", merchant_id="MER_BIG", is_fraud="false") for i in range(2, 60)]

    scores = {r["merchant_id"]: r for r in merchant_risk_scores(bronze_df(spark, rows)).collect()}

    assert scores["MER_NEW"]["merchant_risk_score"] < 0.15, "a single observation must not dominate"
    assert scores["MER_BIG"]["merchant_risk_score"] < 0.02
    assert 0.0 <= scores["MER_NEW"]["merchant_risk_score"] <= 1.0


def test_unknown_merchants_default_to_a_nonzero_risk(spark):
    """A merchant missing from the dimension is new, not safe. Defaulting to zero would
    rank brand-new mule merchants as the safest rows in the table."""
    bronze = bronze_df(spark, [raw_row(transaction_id="T1", merchant_id="MER_UNSEEN")])
    dim = merchant_risk_scores(bronze).filter(F.col("merchant_id") == "NOTHING")

    silver = build_silver(bronze, dim)
    row = silver.collect()[0]

    assert row["merchant_risk_score"] > 0
    assert row["merchant_risk_tier"] == "unknown"


def test_build_silver_produces_every_feature_column(spark):
    rows = [
        raw_row(transaction_id=f"T{i}", timestamp=(BASE + timedelta(minutes=i * 5)).isoformat())
        for i in range(6)
    ]
    silver = build_silver(bronze_df(spark, rows))

    for column in (
        "txn_count_1h",
        "txn_count_24h",
        "amount_zscore_30d",
        "merchant_risk_score",
        "geo_distance_from_prior_km",
        "implied_speed_kmh",
        "device_change_flag",
        "is_high_velocity",
        "is_amount_outlier",
        "is_impossible_travel",
        "fraud_signal_count",
    ):
        assert column in silver.columns, f"missing feature column {column}"


def test_fraud_signal_count_is_bounded(spark):
    rows = [
        raw_row(transaction_id=f"T{i}", timestamp=(BASE + timedelta(minutes=i)).isoformat()) for i in range(8)
    ]
    silver = build_silver(bronze_df(spark, rows))
    for row in silver.collect():
        assert 0 <= row["fraud_signal_count"] <= 4


def test_velocity_burst_trips_the_high_velocity_flag(spark):
    """Six transactions in six minutes — the card-testing pattern the producer injects."""
    rows = [
        raw_row(transaction_id=f"T{i}", timestamp=(BASE + timedelta(minutes=i)).isoformat()) for i in range(6)
    ]
    silver = build_silver(bronze_df(spark, rows))
    last = sorted(silver.collect(), key=lambda r: r["transaction_timestamp"])[-1]
    assert last["is_high_velocity"] is True


# ------------------------------------------------------------------------------ gold


def silver_fixture(spark, rows: list[dict]):
    return build_silver(bronze_df(spark, rows))


def test_fraud_metrics_daily_computes_rates_correctly(spark):
    """Four transactions, one fraudulent, all the same grain: 25% fraud rate."""
    rows = [
        raw_row(transaction_id="T1", amount="100.00", is_fraud="false"),
        raw_row(
            transaction_id="T2",
            amount="100.00",
            is_fraud="false",
            timestamp=(BASE + timedelta(hours=1)).isoformat(),
        ),
        raw_row(
            transaction_id="T3",
            amount="100.00",
            is_fraud="false",
            timestamp=(BASE + timedelta(hours=2)).isoformat(),
        ),
        raw_row(
            transaction_id="T4",
            amount="200.00",
            is_fraud="true",
            timestamp=(BASE + timedelta(hours=3)).isoformat(),
        ),
    ]
    metrics = build_fraud_metrics_daily(silver_fixture(spark, rows)).collect()

    assert len(metrics) == 1
    row = metrics[0]
    assert row["transaction_count"] == 4
    assert row["fraud_transaction_count"] == 1
    assert row["fraud_rate_pct"] == pytest.approx(25.0)
    assert row["total_amount_usd"] == pytest.approx(500.0)
    assert row["fraud_loss_amount_usd"] == pytest.approx(200.0)
    assert row["fraud_loss_rate_pct"] == pytest.approx(40.0)


def test_percentages_stay_within_zero_and_one_hundred(spark):
    """The gold DQ ruleset asserts this too — a fraud_rate_pct of 4500 would be reported
    verbatim and confidently by the agent."""
    rows = [raw_row(transaction_id=f"T{i}", is_fraud="true" if i % 3 == 0 else "false") for i in range(12)]
    for row in build_fraud_metrics_daily(silver_fixture(spark, rows)).collect():
        assert 0 <= row["fraud_rate_pct"] <= 100
        assert 0 <= row["approval_rate_pct"] <= 100


def test_metrics_grain_is_unique(spark):
    """A duplicated (dt, mcc, channel) means the GROUP BY is wrong and every number in
    the table is double-counted."""
    rows = [
        raw_row(transaction_id=f"T{i}", mcc="5411" if i % 2 else "5812", channel="ecommerce")
        for i in range(10)
    ]
    metrics = build_fraud_metrics_daily(silver_fixture(spark, rows))
    grain = [(r["dt"], r["mcc"], r["channel"]) for r in metrics.collect()]
    assert len(grain) == len(set(grain))


def test_approval_rate_uses_auth_response_code_when_present(spark):
    fields = [*RAW_FIELDS, "auth_response_code"]
    codes = ["00", "00", "05", "51"]
    data = []
    for i, code in enumerate(codes):
        row = raw_row(transaction_id=f"T{i}", timestamp=(BASE + timedelta(hours=i)).isoformat())
        row["auth_response_code"] = code
        data.append([row.get(f) for f in fields])

    raw = spark.createDataFrame(data, schema=string_schema(fields))
    valid, _ = split_valid_and_quarantine(raw)
    metrics = build_fraud_metrics_daily(build_silver(valid)).collect()[0]

    assert metrics["approved_transaction_count"] == 2
    assert metrics["approval_rate_pct"] == pytest.approx(50.0)


def test_merchant_risk_rollup(spark):
    rows = [
        raw_row(transaction_id="T1", merchant_id="MER1", amount="100.00", is_fraud="true"),
        raw_row(
            transaction_id="T2",
            merchant_id="MER1",
            amount="100.00",
            is_fraud="false",
            timestamp=(BASE + timedelta(hours=1)).isoformat(),
        ),
        raw_row(
            transaction_id="T3",
            merchant_id="MER2",
            amount="50.00",
            is_fraud="false",
            timestamp=(BASE + timedelta(hours=2)).isoformat(),
        ),
    ]
    risk = {r["merchant_id"]: r for r in build_merchant_risk(silver_fixture(spark, rows)).collect()}

    assert risk["MER1"]["transaction_count"] == 2
    assert risk["MER1"]["fraud_transaction_count"] == 1
    assert risk["MER1"]["fraud_rate_pct"] == pytest.approx(50.0)
    assert risk["MER1"]["fraud_loss_amount_usd"] == pytest.approx(100.0)
    assert risk["MER2"]["fraud_rate_pct"] == pytest.approx(0.0)


def test_gold_survives_a_single_row(spark):
    """Edge case that breaks stddev-based logic — a one-row day must still aggregate."""
    metrics = build_fraud_metrics_daily(silver_fixture(spark, [raw_row()])).collect()
    assert len(metrics) == 1
    assert metrics[0]["transaction_count"] == 1
