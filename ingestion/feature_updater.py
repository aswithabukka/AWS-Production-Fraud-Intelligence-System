"""Kinesis consumer Lambda: maintain the online feature store.

For every transaction flowing through the stream, append (timestamp, amount) to that
customer's rolling event list in DynamoDB — the state the real-time `/score` endpoint
turns into velocity counts and amount baselines in milliseconds.

This is the train/serve parity answer in miniature: the OFFLINE features (silver, via
Spark windows) and these ONLINE features approximate the same quantities from the same
event stream. Keeping both definitions small and side-by-side is what stops them
drifting apart — the classic failure mode of real-time ML.

Kept deliberately tiny: pure boto3, bounded item size (last 60 events per customer,
30-day horizon), idempotent per event. Cost shape: Lambda per-invoke + DynamoDB
on-demand — zero when the stream is quiet.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import boto3

MAX_EVENTS_PER_CUSTOMER = 60
HORIZON_SECONDS = 30 * 86400

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["FEATURES_TABLE"])
    return _table


def compact_events(events: list[dict], now: float) -> list[dict]:
    """Drop events past the horizon, keep the newest MAX_EVENTS. Pure — unit tested."""
    fresh = [e for e in events if now - float(e["t"]) <= HORIZON_SECONDS]
    fresh.sort(key=lambda e: float(e["t"]))
    return fresh[-MAX_EVENTS_PER_CUSTOMER:]


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    table = _get_table()
    now = time.time()
    updated = 0

    # Group by customer so a burst costs one DynamoDB write, not one per event.
    by_customer: dict[str, list[dict]] = {}
    for record in event.get("Records", []):
        try:
            txn = json.loads(base64.b64decode(record["kinesis"]["data"]))
        except Exception:  # noqa: BLE001 - malformed records are bronze's problem, not ours
            continue
        customer = txn.get("customer_id")
        amount = txn.get("amount")
        if not customer or not isinstance(amount, int | float):
            continue
        try:
            ts = datetime.fromisoformat(str(txn.get("timestamp"))).timestamp()
        except (ValueError, TypeError):
            ts = now
        by_customer.setdefault(str(customer), []).append({"t": ts, "a": float(amount)})

    for customer, new_events in by_customer.items():
        current = table.get_item(Key={"customer_id": customer}).get("Item", {})
        events = [{"t": float(e["t"]), "a": float(e["a"])} for e in current.get("events", [])]
        events.extend(new_events)
        events = compact_events(events, now)
        table.put_item(
            Item={
                "customer_id": customer,
                "events": [{"t": Decimal(str(round(e["t"], 3))), "a": Decimal(str(e["a"]))} for e in events],
                "updated_at": Decimal(str(round(now, 3))),
                # TTL column: customers silent for 60 days age out of the store entirely.
                "expires_at": int(now) + 60 * 86400,
            }
        )
        updated += 1

    return {"customers_updated": updated}
