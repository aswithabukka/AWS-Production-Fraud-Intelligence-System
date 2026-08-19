"""fraud_value_daily — the dollars ledger, against hand-computable cases."""

from __future__ import annotations

import pandas as pd
import pytest

from ml.value import VALUE_COLUMNS, fraud_value_daily


@pytest.fixture
def one_day() -> tuple[pd.DataFrame, pd.DataFrame]:
    silver = pd.DataFrame(
        {
            "transaction_id": ["t1", "t2", "t3", "t4", "t5"],
            "dt": ["2026-08-14"] * 5,
            "amount": [100.0, 200.0, 50.0, 1000.0, 25.0],
        }
    )
    scores = pd.DataFrame(
        {
            "transaction_id": ["t1", "t2", "t3", "t4", "t5"],
            #                caught  missed  false-alarm  caught  clean
            "predicted_is_fraud": [True, False, True, True, False],
            "actual_is_fraud": [True, True, False, True, False],
        }
    )
    return silver, scores


def test_confusion_cells_priced_correctly(one_day):
    silver, scores = one_day
    out = fraud_value_daily(silver, scores)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["transaction_count"] == 5
    assert row["actual_fraud_count"] == 3
    assert row["actual_fraud_amount_usd"] == pytest.approx(1300.0)  # t1 + t2 + t4
    assert row["caught_fraud_count"] == 2
    assert row["caught_fraud_amount_usd"] == pytest.approx(1100.0)  # t1 + t4
    assert row["missed_fraud_count"] == 1
    assert row["missed_fraud_amount_usd"] == pytest.approx(200.0)  # t2
    assert row["false_alarm_count"] == 1
    assert row["false_alarm_amount_usd"] == pytest.approx(50.0)  # t3


def test_rates(one_day):
    silver, scores = one_day
    row = fraud_value_daily(silver, scores).iloc[0]

    assert row["capture_rate_pct"] == pytest.approx(66.67, abs=0.01)  # 2 of 3
    assert row["dollar_capture_rate_pct"] == pytest.approx(84.62, abs=0.01)  # 1100/1300
    assert row["flag_precision_pct"] == pytest.approx(66.67, abs=0.01)  # 2 of 3 flags


def test_no_fraud_day_yields_null_rates_not_lies():
    silver = pd.DataFrame(
        {"transaction_id": ["a", "b"], "dt": ["2026-08-15"] * 2, "amount": [10.0, 20.0]}
    )
    scores = pd.DataFrame(
        {
            "transaction_id": ["a", "b"],
            "predicted_is_fraud": [False, False],
            "actual_is_fraud": [False, False],
        }
    )
    row = fraud_value_daily(silver, scores).iloc[0]
    assert pd.isna(row["capture_rate_pct"])
    assert pd.isna(row["flag_precision_pct"])
    assert row["caught_fraud_amount_usd"] == 0.0


def test_multiple_days_stay_separate():
    silver = pd.DataFrame(
        {
            "transaction_id": ["a", "b"],
            "dt": ["2026-08-14", "2026-08-15"],
            "amount": [100.0, 300.0],
        }
    )
    scores = pd.DataFrame(
        {
            "transaction_id": ["a", "b"],
            "predicted_is_fraud": [True, True],
            "actual_is_fraud": [True, True],
        }
    )
    out = fraud_value_daily(silver, scores)
    assert list(out["dt"]) == ["2026-08-14", "2026-08-15"]
    assert list(out["caught_fraud_amount_usd"]) == [100.0, 300.0]


def test_column_contract():
    """These names are the SQL agent's prompt surface and the dashboard's API."""
    silver = pd.DataFrame({"transaction_id": ["a"], "dt": ["2026-08-14"], "amount": [1.0]})
    scores = pd.DataFrame(
        {"transaction_id": ["a"], "predicted_is_fraud": [False], "actual_is_fraud": [False]}
    )
    assert list(fraud_value_daily(silver, scores).columns) == VALUE_COLUMNS
