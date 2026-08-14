"""Real-time scoring tests — the full serve path, offline.

Instead of mocking S3, we train a real (small) ensemble locally and inject it as the
loaded bundle. What's under test is the serving contract: feature layering, column-order
parity with training, all six models contributing, and honest degradation with no
history available.
"""

from __future__ import annotations

import pytest

pytest.importorskip("lightgbm")

from api.scoring import ModelBundle, ScoringService  # noqa: E402
from ml.ensemble import train_and_score  # noqa: E402
from tests.test_ensemble import silver_frame  # noqa: E402, F401 - reuse the fixture


@pytest.fixture(scope="module")
def service(silver_frame) -> ScoringService:  # noqa: F811
    result = train_and_score(silver_frame)
    svc = ScoringService.__new__(ScoringService)  # skip boto3 init entirely
    svc.models_uri = "s3://test/models"
    svc.features_table = ""
    svc.ttl = 3600
    svc._ddb = None
    svc._bundle = ModelBundle(
        models=result.fitted_models,
        threshold=result.threshold,
        feature_names=result.feature_names,
        model_run_id="test-run",
    )
    import threading

    svc._lock = threading.Lock()
    return svc


def test_scores_a_rich_event(service):
    out = service.score(
        {
            "transaction_id": "rt-1",
            "amount": 4200.0,
            "txn_count_1h": 7,
            "txn_count_24h": 15,
            "amount_zscore_30d": 6.5,
            "implied_speed_kmh": 8000.0,
            "device_change_flag": True,
            "channel": "ecommerce",
        }
    )
    assert 0 <= out["ensemble_fraud_score"] <= 1
    assert set(out["model_scores"]) == {
        "lightgbm",
        "xgboost",
        "random_forest",
        "svm",
        "isolation_forest",
        "autoencoder",
    }
    assert out["model_run_id"] == "test-run"
    assert out["latency_ms"] < 5_000


def test_fraud_shaped_event_scores_higher_than_normal(service):
    fraud = service.score(
        {
            "amount": 5000.0,
            "txn_count_1h": 8,
            "amount_zscore_30d": 7.0,
            "implied_speed_kmh": 9000.0,
            "device_change_flag": True,
            "channel": "ecommerce",
        }
    )
    normal = service.score(
        {
            "amount": 35.0,
            "txn_count_1h": 1,
            "amount_zscore_30d": 0.1,
            "implied_speed_kmh": 20.0,
            "device_change_flag": False,
            "channel": "card_present",
        }
    )
    assert fraud["ensemble_fraud_score"] > normal["ensemble_fraud_score"]


def test_minimal_event_degrades_honestly(service):
    """Amount alone must still score — absent history becomes missing-indicators, the
    signal the models were trained to interpret."""
    out = service.score({"amount": 50.0})
    assert 0 <= out["ensemble_fraud_score"] <= 1
    assert out["features_from_store"] == []


def test_explanations_present_for_risky_event(service):
    out = service.score(
        {
            "amount": 6000.0,
            "txn_count_1h": 9,
            "amount_zscore_30d": 8.0,
            "implied_speed_kmh": 9500.0,
            "device_change_flag": True,
            "channel": "ecommerce",
        }
    )
    assert isinstance(out["top_risk_factors"], list)
    assert len(out["top_risk_factors"]) >= 1


def test_prediction_follows_threshold(service):
    out = service.score({"amount": 100.0, "channel": "card_present"})
    assert out["predicted_is_fraud"] == (out["ensemble_fraud_score"] >= out["decision_threshold"])


def test_feature_store_math():
    """The online velocity computation, against hand-computable state."""
    svc = ScoringService.__new__(ScoringService)
    svc.features_table = "x"

    import time as _time

    now = _time.time()

    class FakeTable:
        def get_item(self, Key):
            return {
                "Item": {
                    "customer_id": Key["customer_id"],
                    "events": [
                        {"t": now - 100, "a": 10.0},  # in 1h and 24h
                        {"t": now - 3000, "a": 20.0},  # in 1h and 24h
                        {"t": now - 40000, "a": 30.0},  # 24h only
                        {"t": now - 200000, "a": 40.0},  # 30d only
                    ],
                }
            }

    svc._ddb = FakeTable()
    feats = svc.customer_features("CUST1")
    assert feats["txn_count_1h"] == 3  # 2 prior + the event being scored
    assert feats["txn_count_24h"] == 4  # 3 prior + current
    assert feats["prior_txn_count_30d"] == 4
    assert feats["amount_sum_1h"] == 30.0
    assert feats["amount_sum_24h"] == 60.0


def test_compact_events_prunes_and_caps():
    from ingestion.feature_updater import HORIZON_SECONDS, MAX_EVENTS_PER_CUSTOMER, compact_events

    now = 1_000_000_000.0
    events = [{"t": now - i * 1000, "a": 1.0} for i in range(100)]
    events.append({"t": now - HORIZON_SECONDS - 10, "a": 99.0})  # too old
    out = compact_events(events, now)
    assert len(out) == MAX_EVENTS_PER_CUSTOMER
    assert all(now - e["t"] <= HORIZON_SECONDS for e in out)
    # newest events survive
    assert out[-1]["t"] == now
