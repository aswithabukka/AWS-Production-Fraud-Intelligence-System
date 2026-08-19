"""The business-value ledger: what the ensemble's decisions are worth in dollars.

Confusion-matrix cells, priced. Per day:

    caught      predicted fraud AND actually fraud   -> dollars intercepted
    missed      actual fraud the model let through   -> dollars lost
    false alarm predicted fraud on a clean txn       -> analyst review workload

This is deliberately a GOLD TABLE, not a dashboard-side calculation: leadership
questions ("how much did we save last week?") deserve a governed, queryable answer the
SQL agent can also cite — not arithmetic buried in a chart's JavaScript.

The honest caveat, stated where it belongs: "intercepted" assumes a flagged fraudulent
transaction would have completed without the flag. That is the standard industry framing
for fraud-prevention value, and it is stated on the dashboard rather than hidden.
"""

from __future__ import annotations

import pandas as pd

VALUE_COLUMNS = [
    "dt",
    "transaction_count",
    "total_amount_usd",
    "actual_fraud_count",
    "actual_fraud_amount_usd",
    "flagged_count",
    "caught_fraud_count",
    "caught_fraud_amount_usd",
    "missed_fraud_count",
    "missed_fraud_amount_usd",
    "false_alarm_count",
    "false_alarm_amount_usd",
    "capture_rate_pct",
    "dollar_capture_rate_pct",
    "flag_precision_pct",
]


def fraud_value_daily(silver: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    """Join per-transaction verdicts back to amounts and roll up per day.

    `silver` needs transaction_id, dt, amount. `scores` needs transaction_id,
    predicted_is_fraud, actual_is_fraud (both from the ensemble run).
    """
    frame = silver[["transaction_id", "dt", "amount"]].merge(
        scores[["transaction_id", "predicted_is_fraud", "actual_is_fraud"]],
        on="transaction_id",
        how="inner",
    )
    frame["amount"] = frame["amount"].astype(float)
    predicted = frame["predicted_is_fraud"].astype(bool)
    actual = frame["actual_is_fraud"].astype(bool)

    frame["_caught"] = predicted & actual
    frame["_missed"] = actual & ~predicted
    frame["_false_alarm"] = predicted & ~actual

    def _sum_where(col: str, mask_col: str) -> pd.Series:
        return frame[col].where(frame[mask_col], 0.0)

    frame["_caught_amount"] = _sum_where("amount", "_caught")
    frame["_missed_amount"] = _sum_where("amount", "_missed")
    frame["_false_alarm_amount"] = _sum_where("amount", "_false_alarm")
    frame["_actual_amount"] = frame["amount"].where(actual, 0.0)

    daily = (
        frame.groupby("dt", as_index=False)
        .agg(
            transaction_count=("transaction_id", "count"),
            total_amount_usd=("amount", "sum"),
            actual_fraud_count=("actual_is_fraud", "sum"),
            actual_fraud_amount_usd=("_actual_amount", "sum"),
            flagged_count=("predicted_is_fraud", "sum"),
            caught_fraud_count=("_caught", "sum"),
            caught_fraud_amount_usd=("_caught_amount", "sum"),
            missed_fraud_count=("_missed", "sum"),
            missed_fraud_amount_usd=("_missed_amount", "sum"),
            false_alarm_count=("_false_alarm", "sum"),
            false_alarm_amount_usd=("_false_alarm_amount", "sum"),
        )
        .sort_values("dt")
    )

    def _pct(numer: pd.Series, denom: pd.Series) -> pd.Series:
        # NULL, not 0 or 100, when the denominator is empty: "no fraud that day" is not
        # the same statement as "caught 0% of it".
        return (100.0 * numer / denom.where(denom > 0)).round(2)

    daily["capture_rate_pct"] = _pct(daily["caught_fraud_count"], daily["actual_fraud_count"])
    daily["dollar_capture_rate_pct"] = _pct(
        daily["caught_fraud_amount_usd"], daily["actual_fraud_amount_usd"]
    )
    daily["flag_precision_pct"] = _pct(daily["caught_fraud_count"], daily["flagged_count"])

    for col in (
        "total_amount_usd",
        "actual_fraud_amount_usd",
        "caught_fraud_amount_usd",
        "missed_fraud_amount_usd",
        "false_alarm_amount_usd",
    ):
        daily[col] = daily[col].round(2)
    for col in (
        "actual_fraud_count",
        "flagged_count",
        "caught_fraud_count",
        "missed_fraud_count",
        "false_alarm_count",
    ):
        daily[col] = daily[col].astype(int)

    return daily[VALUE_COLUMNS]
