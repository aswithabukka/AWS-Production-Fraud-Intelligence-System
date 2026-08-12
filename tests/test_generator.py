"""Tests for the synthetic transaction generator.

These run with no AWS credentials and no network. They exist to guarantee the data
contract the bronze/silver jobs are written against — if the generator drifts, these fail
before a Glue job does.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from ingestion.entities import haversine_km
from ingestion.generator import GeneratorConfig, TransactionGenerator
from ingestion.producer import corrupt, main

REQUIRED_FIELDS = {
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
}


@pytest.fixture
def generator() -> TransactionGenerator:
    return TransactionGenerator(GeneratorConfig(seed=42, n_customers=200, n_merchants=60))


def test_every_event_has_the_raw_contract(generator: TransactionGenerator) -> None:
    for event in generator.stream(500):
        assert event.keys() >= REQUIRED_FIELDS


def test_events_are_json_serialisable(generator: TransactionGenerator) -> None:
    for event in generator.stream(100):
        assert json.loads(json.dumps(event)) == event


def test_generator_is_deterministic_under_a_seed() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    a = list(TransactionGenerator(GeneratorConfig(seed=7, n_customers=50, n_merchants=20)).stream(50, start))
    b = list(TransactionGenerator(GeneratorConfig(seed=7, n_customers=50, n_merchants=20)).stream(50, start))
    # ingest_timestamp is wall-clock, so compare everything else.
    strip = lambda events: [{k: v for k, v in e.items() if k != "ingest_timestamp"} for e in events]  # noqa: E731
    assert strip(a) == strip(b)


def test_transaction_ids_are_unique(generator: TransactionGenerator) -> None:
    ids = [e["transaction_id"] for e in generator.stream(3_000)]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("target_rate", [0.0, 0.01, 0.05, 0.2])
def test_fraud_rate_is_honoured(target_rate: float) -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=11, fraud_rate=target_rate, n_customers=300))
    events = list(gen.stream(6_000))
    observed = sum(e["is_fraud"] for e in events) / len(events)
    if target_rate == 0.0:
        assert observed == 0.0
    else:
        # Velocity bursts emit several fraud rows per draw, so the observed share runs
        # above the per-draw probability by design. Assert the ordering, not equality.
        assert observed >= target_rate * 0.9
        assert observed <= target_rate * 6


def test_amounts_are_positive_and_bounded(generator: TransactionGenerator) -> None:
    for event in generator.stream(2_000):
        assert 0 < event["amount"] <= 250_000


def test_coordinates_are_valid(generator: TransactionGenerator) -> None:
    for event in generator.stream(1_000):
        assert -90 <= event["lat"] <= 90
        assert -180 <= event["lon"] <= 180


def test_timestamps_are_iso8601_utc(generator: TransactionGenerator) -> None:
    for event in generator.stream(200):
        parsed = datetime.fromisoformat(event["timestamp"])
        assert parsed.tzinfo is not None


def test_all_three_anomaly_archetypes_are_produced() -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=3, fraud_rate=0.25, n_customers=200))
    kinds = Counter(e["anomaly_type"] for e in gen.stream(2_000) if e["is_fraud"])
    assert set(kinds) == {"velocity", "impossible_geo", "amount_outlier"}


def test_velocity_burst_is_clustered_in_time_and_customer() -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=5, fraud_rate=1.0, n_customers=100))
    events = [e for e in gen.stream(400) if e["anomaly_type"] == "velocity"]
    assert events, "expected at least one velocity burst"

    # A burst's identity is the compromised device — the same customer can be hit by
    # more than one burst in a long run, so grouping by customer alone would merge them.
    by_device: dict[str, list[dict]] = {}
    for event in events:
        by_device.setdefault(event["device_id"], []).append(event)

    bursts = [rows for rows in by_device.values() if len(rows) >= 4]
    assert bursts, "expected a burst of >= 4 transactions on one device"
    for rows in bursts:
        # One card, one compromised device, one tight window.
        assert len({r["customer_id"] for r in rows}) == 1
        stamps = sorted(datetime.fromisoformat(r["timestamp"]) for r in rows)
        assert stamps[-1] - stamps[0] < timedelta(hours=1)


def test_impossible_geo_is_actually_far_away() -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=9, fraud_rate=1.0, n_customers=100))
    geo_events = [e for e in gen.stream(300) if e["anomaly_type"] == "impossible_geo"]
    assert geo_events

    home = {c.customer_id: (c.home_lat, c.home_lon) for c in gen.customers}
    for event in geo_events:
        lat, lon = home[event["customer_id"]]
        # Offshore cities are all well over 1000 km from any US metro in the catalog.
        assert haversine_km(lat, lon, event["lat"], event["lon"]) > 1_000


def test_amount_outliers_are_large() -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=13, fraud_rate=1.0, n_customers=150))
    events = list(gen.stream(1_500))
    outliers = [e["amount"] for e in events if e["anomaly_type"] == "amount_outlier"]
    legit_median = sorted(e["amount"] for e in events if not e["is_fraud"]) or [50.0]
    median = legit_median[len(legit_median) // 2]
    assert outliers
    assert sorted(outliers)[len(outliers) // 2] > median


def test_fraud_skews_card_not_present() -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=17, fraud_rate=0.3, n_customers=300))
    events = list(gen.stream(4_000))
    fraud_ecom = sum(1 for e in events if e["is_fraud"] and e["channel"] == "ecommerce")
    fraud_total = sum(1 for e in events if e["is_fraud"])
    legit_ecom = sum(1 for e in events if not e["is_fraud"] and e["channel"] == "ecommerce")
    legit_total = sum(1 for e in events if not e["is_fraud"])
    assert fraud_ecom / fraud_total > legit_ecom / legit_total


def test_merchant_ids_all_resolve(generator: TransactionGenerator) -> None:
    """The bronze referential-integrity rule joins on this — clean data must never
    violate it, or the quarantine demo has no signal."""
    known = {m.merchant_id for m in generator.merchants}
    for event in generator.stream(1_000):
        assert event["merchant_id"] in known


# ------------------------------------------------------------------ schema evolution


def test_schema_version_1_omits_the_evolution_field() -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=1, schema_version=1, n_customers=50))
    for event in gen.stream(200):
        assert "auth_response_code" not in event
        assert event["schema_version"] == 1


def test_schema_version_2_adds_auth_response_code() -> None:
    gen = TransactionGenerator(GeneratorConfig(seed=1, schema_version=2, n_customers=50))
    events = list(gen.stream(200))
    assert all("auth_response_code" in e for e in events)
    assert all(e["schema_version"] == 2 for e in events)
    # v2 must be a pure superset of v1 — Iceberg schema evolution only handles adds.
    v1_fields = set(
        TransactionGenerator(GeneratorConfig(seed=1, schema_version=1, n_customers=50)).next_event().keys()
    )
    assert v1_fields < set(events[0].keys())


# ----------------------------------------------------------------------- corruption


def test_corrupt_produces_every_failure_mode() -> None:
    import random

    gen = TransactionGenerator(GeneratorConfig(seed=2, n_customers=50))
    rng = random.Random(0)
    modes = Counter(corrupt(gen.next_event(), rng)["_corruption_mode"] for _ in range(400))
    assert len(modes) == 7, f"expected all 7 corruption modes, saw {sorted(modes)}"


def test_corrupt_leaves_the_original_untouched() -> None:
    import random

    gen = TransactionGenerator(GeneratorConfig(seed=2, n_customers=50))
    event = gen.next_event()
    snapshot = dict(event)
    corrupt(event, random.Random(0))
    assert event == snapshot


# ---------------------------------------------------------------------------- CLI


def test_cli_dry_run_writes_jsonl(tmp_path, capsys) -> None:
    out = tmp_path / "events.jsonl"
    exit_code = main(["--out", str(out), "--count", "120", "--rate", "0", "--seed", "4"])
    assert exit_code == 0
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 120
    assert json.loads(lines[0]).keys() >= REQUIRED_FIELDS


def test_cli_replay_reproduces_identical_transaction_ids(tmp_path) -> None:
    source = tmp_path / "batch.jsonl"
    main(["--out", str(source), "--count", "80", "--rate", "0", "--seed", "6"])

    replayed = tmp_path / "replay.jsonl"
    main(["--out", str(replayed), "--replay", str(source), "--rate", "0"])

    original_ids = [json.loads(line)["transaction_id"] for line in source.read_text().splitlines()]
    replay_ids = [json.loads(line)["transaction_id"] for line in replayed.read_text().splitlines()]
    assert original_ids == replay_ids


def test_cli_requires_a_sink(capsys) -> None:
    assert main(["--count", "1"]) == 2
