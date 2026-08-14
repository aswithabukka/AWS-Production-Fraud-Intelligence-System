"""API surface tests.

No AWS: the graph is stubbed and metric publishing is disabled. What is under test is the
HTTP contract — status codes, validation, and the shape of what comes back.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from api import main as api_main  # noqa: E402


@pytest.fixture(autouse=True)
def no_metrics(monkeypatch):
    """Telemetry must never be the reason a test — or a user request — fails."""
    monkeypatch.setattr(api_main, "_publish_metrics", lambda *a, **k: None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def test_health_reports_configuration(client):
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] in ("ok", "degraded")
    assert "athena_workgroup" in body["checks"]
    assert "guardrail" in body["checks"]


def test_ask_returns_the_full_answer_envelope(client, monkeypatch):
    monkeypatch.setattr(
        api_main,
        "ask",
        lambda question, **kwargs: {
            "question": question,
            "answer": "Fraud rate was 1.5%.",
            "tools_used": ["query_lakehouse"],
            "sql_executed": ["SELECT dt FROM fraud_gold.fraud_metrics_daily LIMIT 100"],
            "citations": [],
            "iterations": 1,
            "stopped_reason": None,
            "latency_ms": 42.0,
            "token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "model_calls": 2},
            "trace": [{"node": "route_query"}],
        },
    )

    response = client.post("/ask", json={"question": "what was the fraud rate?"})
    assert response.status_code == 200

    body = response.json()
    assert body["answer"] == "Fraud rate was 1.5%."
    # The executed SQL travels with the answer — an analytics answer nobody can check is
    # worth less than no answer.
    assert body["sql_executed"][0].startswith("SELECT")
    assert body["token_usage"]["total_tokens"] == 15


def test_ask_rejects_a_too_short_question(client):
    assert client.post("/ask", json={"question": "hi"}).status_code == 422


def test_ask_rejects_an_oversized_question(client):
    """The question is prepended to every routing turn, so its length is a cost control."""
    assert client.post("/ask", json={"question": "x" * 5_000}).status_code == 422


def test_ask_surfaces_graph_failures_as_500(client, monkeypatch):
    def explode(_question, **_kwargs):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(api_main, "ask", explode)
    response = client.post("/ask", json={"question": "anything at all"})

    assert response.status_code == 500
    assert "bedrock unavailable" in response.json()["detail"]


def test_sql_check_accepts_a_gold_query(client):
    response = client.post(
        "/sql/check",
        json={"sql": "SELECT dt, fraud_rate_pct FROM fraud_gold.fraud_metrics_daily"},
    )
    body = response.json()

    assert body["status"] == "ok"
    assert body["limit_was_injected"] is True
    assert body["tables"] == ["fraud_gold.fraud_metrics_daily"]


def test_sql_check_rejects_a_write(client):
    body = client.post("/sql/check", json={"sql": "DROP TABLE fraud_gold.merchant_risk"}).json()
    assert body["status"] == "rejected"
    assert "not permitted" in body["reason"]


def test_sql_check_rejects_a_silver_read(client):
    body = client.post("/sql/check", json={"sql": "SELECT * FROM fraud_silver.transactions"}).json()
    assert body["status"] == "rejected"
    assert "allowlist" in body["reason"]


def test_metrics_endpoint_counts_activity(client):
    client.post("/sql/check", json={"sql": "DROP TABLE fraud_gold.merchant_risk"})
    counters = client.get("/metrics").json()["counters"]
    assert counters.get("sql_rejected", 0) >= 1


def test_root_serves_the_console(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "FRAUD" in response.text
    # The console must reference the real endpoints it drives.
    for endpoint in ("/ask", "/sql/check", "/health"):
        assert endpoint in response.text
