"""Team dashboards over the gold layer — served by the same container as everything else.

Three audiences, three views, zero new infrastructure:

    ops       fraud analysts    volumes, fraud rate trend, where fraud concentrates
    model     the ML team       per-model quality over time, feedback-loop impact
    business  leadership        dollars intercepted / missed / review workload

Why not QuickSight/Grafana: both bill per user per month for what is, here, four Athena
queries and some SVG. The gold tables are small, the workgroup already caps any query at
1 GB scanned, and a 5-minute cache means a team staring at the dashboard all day costs
cents. Same pricing shape as the rest of the platform: per-request, zero at rest.

Every number on screen is also a governed table the SQL agent can cite — the dashboard
and the agent disagree about nothing, because they read the same gold.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from agents.tools.sql_tool import run_athena_query

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# The queries are fixed strings, not user input — the dashboard has no query surface.
# (Anything user-shaped goes through the SQL validator; this module never takes input.)
_QUERIES = {
    "daily": """
        SELECT dt,
               SUM(transaction_count)             AS transaction_count,
               SUM(fraud_transaction_count)       AS fraud_transaction_count,
               ROUND(SUM(total_amount_usd), 2)    AS total_amount_usd,
               ROUND(SUM(fraud_loss_amount_usd), 2) AS fraud_loss_amount_usd,
               ROUND(100.0 * SUM(fraud_transaction_count) / SUM(transaction_count), 3)
                                                  AS fraud_rate_pct
        FROM fraud_gold.fraud_metrics_daily
        GROUP BY dt ORDER BY dt
    """,
    "channel": """
        SELECT channel,
               SUM(transaction_count)       AS transaction_count,
               SUM(fraud_transaction_count) AS fraud_transaction_count,
               ROUND(100.0 * SUM(fraud_transaction_count) / SUM(transaction_count), 3)
                                            AS fraud_rate_pct
        FROM fraud_gold.fraud_metrics_daily
        GROUP BY channel ORDER BY fraud_rate_pct DESC
    """,
    "mcc": """
        SELECT mcc,
               SUM(fraud_transaction_count)         AS fraud_transaction_count,
               ROUND(SUM(fraud_loss_amount_usd), 2) AS fraud_loss_amount_usd
        FROM fraud_gold.fraud_metrics_daily
        GROUP BY mcc ORDER BY fraud_loss_amount_usd DESC LIMIT 8
    """,
    "model_metrics": """
        SELECT model_name, model_run_id, trained_at, training_rows,
               holdout_roc_auc, holdout_precision, holdout_recall, holdout_f1,
               feedback_labels_confirmed, feedback_labels_changed
        FROM fraud_gold.model_metrics
        ORDER BY trained_at, model_name
    """,
    "value": """
        SELECT dt, transaction_count, flagged_count,
               actual_fraud_count, actual_fraud_amount_usd,
               caught_fraud_count, caught_fraud_amount_usd,
               missed_fraud_count, missed_fraud_amount_usd,
               false_alarm_count, false_alarm_amount_usd,
               capture_rate_pct, dollar_capture_rate_pct, flag_precision_pct
        FROM fraud_gold.fraud_value_daily
        ORDER BY dt
    """,
}


def _query(name: str, runner: Callable[[str], dict] | None = None) -> dict:
    """One Athena round-trip per query per 5 minutes, no matter how many viewers."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    # Resolved at call time, not bound at import — so tests can stub the module attr.
    result = (runner or run_athena_query)(_QUERIES[name])
    with _cache_lock:
        _cache[name] = (now, result)
    return result


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ------------------------------------------------------------------ shaping (pure)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any) -> int:
    return int(_f(value))


def shape_ops(daily: list[dict], channel: list[dict], mcc: list[dict]) -> dict:
    total_txns = sum(_i(r["transaction_count"]) for r in daily)
    total_fraud = sum(_i(r["fraud_transaction_count"]) for r in daily)
    return {
        "tiles": {
            "transactions": total_txns,
            "fraud_transactions": total_fraud,
            "fraud_rate_pct": round(100.0 * total_fraud / total_txns, 2) if total_txns else None,
            "fraud_loss_usd": round(sum(_f(r["fraud_loss_amount_usd"]) for r in daily), 2),
            "days": len(daily),
        },
        "fraud_rate_trend": [
            {
                "dt": r["dt"],
                "fraud_rate_pct": _f(r["fraud_rate_pct"]),
                "transactions": _i(r["transaction_count"]),
            }
            for r in daily
        ],
        "by_channel": [
            {
                "channel": r["channel"],
                "transactions": _i(r["transaction_count"]),
                "fraud_rate_pct": _f(r["fraud_rate_pct"]),
            }
            for r in channel
        ],
        "loss_by_mcc": [
            {"mcc": r["mcc"], "fraud_loss_usd": _f(r["fraud_loss_amount_usd"])} for r in mcc
        ],
    }


def shape_model(metrics: list[dict]) -> dict:
    runs = sorted({r["model_run_id"] for r in metrics})
    latest = runs[-1] if runs else None
    leaderboard = sorted(
        (
            {
                "model": r["model_name"],
                "roc_auc": _f(r["holdout_roc_auc"]),
                "precision": _f(r["holdout_precision"]),
                "recall": _f(r["holdout_recall"]),
                "f1": _f(r["holdout_f1"]),
            }
            for r in metrics
            if r["model_run_id"] == latest
        ),
        key=lambda r: -r["roc_auc"],
    )
    trend: dict[str, list] = {}
    for r in metrics:
        trend.setdefault(r["model_name"], []).append(
            {"run": r["model_run_id"], "roc_auc": _f(r["holdout_roc_auc"])}
        )
    feedback = [
        {
            "run": run,
            "training_rows": next(_i(r["training_rows"]) for r in metrics if r["model_run_id"] == run),
            "labels_confirmed": next(
                _i(r.get("feedback_labels_confirmed", 0)) for r in metrics if r["model_run_id"] == run
            ),
            "labels_changed": next(
                _i(r.get("feedback_labels_changed", 0)) for r in metrics if r["model_run_id"] == run
            ),
        }
        for run in runs
    ]
    return {
        "latest_run": latest,
        "training_runs": len(runs),
        "leaderboard": leaderboard,
        "auc_trend": trend,
        "feedback": feedback,
    }


def shape_business(value: list[dict]) -> dict:
    caught = round(sum(_f(r["caught_fraud_amount_usd"]) for r in value), 2)
    missed = round(sum(_f(r["missed_fraud_amount_usd"]) for r in value), 2)
    actual = round(sum(_f(r["actual_fraud_amount_usd"]) for r in value), 2)
    caught_n = sum(_i(r["caught_fraud_count"]) for r in value)
    actual_n = sum(_i(r["actual_fraud_count"]) for r in value)
    flagged_n = sum(_i(r["flagged_count"]) for r in value)
    return {
        "tiles": {
            "intercepted_usd": caught,
            "missed_usd": missed,
            "dollar_capture_rate_pct": round(100.0 * caught / actual, 2) if actual else None,
            "capture_rate_pct": round(100.0 * caught_n / actual_n, 2) if actual_n else None,
            "flag_precision_pct": round(100.0 * caught_n / flagged_n, 2) if flagged_n else None,
            "review_queue": sum(_i(r["false_alarm_count"]) for r in value),
        },
        "daily": [
            {
                "dt": r["dt"],
                "caught_usd": _f(r["caught_fraud_amount_usd"]),
                "missed_usd": _f(r["missed_fraud_amount_usd"]),
                "false_alarms": _i(r["false_alarm_count"]),
                "capture_rate_pct": _f(r["capture_rate_pct"], default=float("nan")),
            }
            for r in value
        ],
    }


# ------------------------------------------------------------------ entry points


def ops_dashboard() -> dict:
    return shape_ops(
        _query("daily")["rows"], _query("channel")["rows"], _query("mcc")["rows"]
    )


def model_dashboard() -> dict:
    return shape_model(_query("model_metrics")["rows"])


def business_dashboard() -> dict:
    return shape_business(_query("value")["rows"])
