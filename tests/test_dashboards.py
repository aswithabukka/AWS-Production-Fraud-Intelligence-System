"""Dashboard shaping + HTTP contract. No AWS: Athena is stubbed at the query layer."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from api import dashboards  # noqa: E402
from api import main as api_main  # noqa: E402
from api.dashboards import shape_business, shape_model, shape_ops  # noqa: E402

DAILY = [
    {"dt": "2026-08-13", "transaction_count": "1000", "fraud_transaction_count": "30",
     "total_amount_usd": "50000.0", "fraud_loss_amount_usd": "9000.0", "fraud_rate_pct": "3.0"},
    {"dt": "2026-08-14", "transaction_count": "2000", "fraud_transaction_count": "80",
     "total_amount_usd": "90000.0", "fraud_loss_amount_usd": "21000.0", "fraud_rate_pct": "4.0"},
]
CHANNEL = [
    {"channel": "ecommerce", "transaction_count": "1800", "fraud_transaction_count": "90",
     "fraud_rate_pct": "5.0"},
    {"channel": "card_present", "transaction_count": "1200", "fraud_transaction_count": "20",
     "fraud_rate_pct": "1.667"},
]
MCC = [{"mcc": "5732", "fraud_transaction_count": "40", "fraud_loss_amount_usd": "15000.0"}]

METRICS = [
    {"model_name": "lightgbm", "model_run_id": "r1", "trained_at": "t", "training_rows": "9000",
     "holdout_roc_auc": "0.95", "holdout_precision": "0.9", "holdout_recall": "0.8", "holdout_f1": "0.85",
     "feedback_labels_confirmed": "0", "feedback_labels_changed": "0"},
    {"model_name": "xgboost", "model_run_id": "r1", "trained_at": "t", "training_rows": "9000",
     "holdout_roc_auc": "0.97", "holdout_precision": "0.92", "holdout_recall": "0.82", "holdout_f1": "0.87",
     "feedback_labels_confirmed": "0", "feedback_labels_changed": "0"},
    {"model_name": "xgboost", "model_run_id": "r2", "trained_at": "u", "training_rows": "11000",
     "holdout_roc_auc": "0.975", "holdout_precision": "0.93", "holdout_recall": "0.84", "holdout_f1": "0.88",
     "feedback_labels_confirmed": "800", "feedback_labels_changed": "41"},
]

VALUE = [
    {"dt": "2026-08-14", "transaction_count": "2000", "flagged_count": "100",
     "actual_fraud_count": "80", "actual_fraud_amount_usd": "21000.0",
     "caught_fraud_count": "70", "caught_fraud_amount_usd": "19000.0",
     "missed_fraud_count": "10", "missed_fraud_amount_usd": "2000.0",
     "false_alarm_count": "30", "false_alarm_amount_usd": "1500.0",
     "capture_rate_pct": "87.5", "dollar_capture_rate_pct": "90.48", "flag_precision_pct": "70.0"},
]


def test_shape_ops_totals_and_trend():
    out = shape_ops(DAILY, CHANNEL, MCC)
    assert out["tiles"]["transactions"] == 3000
    assert out["tiles"]["fraud_transactions"] == 110
    assert out["tiles"]["fraud_rate_pct"] == pytest.approx(3.67, abs=0.01)
    assert out["tiles"]["fraud_loss_usd"] == pytest.approx(30000.0)
    assert [p["dt"] for p in out["fraud_rate_trend"]] == ["2026-08-13", "2026-08-14"]
    assert out["by_channel"][0]["channel"] == "ecommerce"


def test_shape_model_leaderboard_uses_latest_run_only():
    out = shape_model(METRICS)
    assert out["latest_run"] == "r2"
    assert [r["model"] for r in out["leaderboard"]] == ["xgboost"]  # only model in r2
    assert out["leaderboard"][0]["roc_auc"] == pytest.approx(0.975)
    assert out["feedback"][-1]["labels_changed"] == 41
    assert len(out["auc_trend"]["xgboost"]) == 2


def test_shape_business_prices_the_confusion_matrix():
    out = shape_business(VALUE)
    assert out["tiles"]["intercepted_usd"] == pytest.approx(19000.0)
    assert out["tiles"]["missed_usd"] == pytest.approx(2000.0)
    assert out["tiles"]["dollar_capture_rate_pct"] == pytest.approx(90.48, abs=0.01)
    assert out["tiles"]["review_queue"] == 30
    assert out["daily"][0]["caught_usd"] == pytest.approx(19000.0)


def test_shapes_survive_empty_gold():
    """A fresh deployment has no rows yet — the dashboard must render, not crash."""
    assert shape_ops([], [], [])["tiles"]["fraud_rate_pct"] is None
    assert shape_model([])["leaderboard"] == []
    assert shape_business([])["tiles"]["dollar_capture_rate_pct"] is None


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(api_main, "_publish_metrics", lambda *a, **k: None, raising=False)

    def fake_runner(sql: str) -> dict:
        if "fraud_metrics_daily" in sql and "GROUP BY dt" in sql:
            return {"rows": DAILY}
        if "GROUP BY channel" in sql:
            return {"rows": CHANNEL}
        if "GROUP BY mcc" in sql:
            return {"rows": MCC}
        if "model_metrics" in sql:
            return {"rows": METRICS}
        if "fraud_value_daily" in sql:
            return {"rows": VALUE}
        raise AssertionError(f"unexpected query: {sql}")

    dashboards.clear_cache()
    monkeypatch.setattr(dashboards, "run_athena_query", fake_runner)
    yield TestClient(api_main.app)
    dashboards.clear_cache()


def test_all_three_endpoints_answer(client):
    for name, key in (("ops", "fraud_rate_trend"), ("model", "leaderboard"), ("business", "daily")):
        body = client.get(f"/api/dashboards/{name}").json()
        assert key in body, name


def test_dashboard_failure_is_a_502_with_a_reason(client, monkeypatch):
    def explode(sql: str) -> dict:
        raise RuntimeError("TABLE_NOT_FOUND fraud_value_daily")

    dashboards.clear_cache()
    monkeypatch.setattr(dashboards, "run_athena_query", explode)
    response = client.get("/api/dashboards/business")
    assert response.status_code == 502
    assert "fraud_value_daily" in response.json()["detail"]


def test_dashboards_page_serves(client):
    response = client.get("/dashboards")
    assert response.status_code == 200
    assert "Team dashboards" in response.text
    for endpoint in ("/api/dashboards/", "Business Value", "Model Health"):
        assert endpoint in response.text


def test_query_cache_prevents_athena_hammering(monkeypatch):
    calls = []

    def counting_runner(sql: str) -> dict:
        calls.append(sql)
        return {"rows": []}

    dashboards.clear_cache()
    dashboards._query("daily", runner=counting_runner)
    dashboards._query("daily", runner=counting_runner)
    assert len(calls) == 1
    dashboards.clear_cache()
