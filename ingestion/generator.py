"""Synthetic card-transaction generator.

Emits one flat dict per transaction, matching the raw-zone contract:

    transaction_id, customer_id, merchant_id, mcc, amount, currency, timestamp,
    lat, lon, device_id, channel, is_fraud

Plus two operational fields the bronze job relies on:

    ingest_timestamp  - producer-side emit time; bronze dedupes on
                        (transaction_id, max(ingest_timestamp))
    schema_version    - 1 by default; 2 adds `auth_response_code`, which is how the
                        Iceberg schema-evolution scenario in slice 1c is triggered.

Fraud is not sprinkled uniformly. Three anomaly archetypes are injected, each of which a
specific silver-layer feature is meant to catch:

    velocity      - a burst of transactions for one customer inside a short window
                    -> caught by the 1h/24h per-customer velocity counters
    impossible_geo- a transaction thousands of km from the customer's previous one, minutes
                    apart -> caught by geo-distance / implied-speed
    amount_outlier- an amount far above the customer's trailing-30d distribution
                    -> caught by the amount z-score

Having the generator and the feature layer designed as a matched pair is deliberate: it
means the silver features can be *evaluated*, not just computed.
"""

from __future__ import annotations

import math
import random
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ingestion.entities import (
    CHANNELS,
    CURRENCIES,
    FRAUD_CHANNELS,
    HIGH_RISK_MCCS,
    MCC_CATALOG,
    OFFSHORE_CITIES,
    Customer,
    Merchant,
    build_customers,
    build_merchants,
    jitter_location,
    weighted_choice,
)

ANOMALY_TYPES = ("velocity", "impossible_geo", "amount_outlier")

# Relative likelihood of each archetype when a transaction is flagged fraudulent.
ANOMALY_WEIGHTS = (0.35, 0.30, 0.35)

AUTH_RESPONSE_CODES: list[tuple[str, float]] = [
    ("00", 92.0),  # approved
    ("05", 3.5),  # do not honor
    ("51", 2.5),  # insufficient funds
    ("14", 0.8),  # invalid card number
    ("54", 0.7),  # expired card
    ("59", 0.5),  # suspected fraud
]


@dataclass
class _CustomerState:
    """Mutable per-customer memory the generator needs to make events *correlated*
    rather than independent draws — without it, velocity and geo anomalies are
    impossible to construct."""

    last_lat: float
    last_lon: float
    last_ts: datetime
    last_device_id: str
    recent_amounts: deque[float] = field(default_factory=lambda: deque(maxlen=40))


@dataclass
class GeneratorConfig:
    fraud_rate: float = 0.015
    n_customers: int = 2_000
    n_merchants: int = 500
    schema_version: int = 1
    seed: int | None = None
    # Probability a legitimate transaction happens on a device other than the
    # customer's primary — keeps the device-change flag from being a perfect predictor.
    device_change_rate: float = 0.03


class TransactionGenerator:
    """Deterministic when `seed` is set — the pytest fixtures depend on that."""

    def __init__(self, config: GeneratorConfig | None = None) -> None:
        self.config = config or GeneratorConfig()
        self.rng = random.Random(self.config.seed)
        self.merchants: list[Merchant] = build_merchants(self.rng, self.config.n_merchants)
        self.customers: list[Customer] = build_customers(self.rng, self.config.n_customers)
        self._by_risk: dict[str, list[Merchant]] = {"low": [], "medium": [], "high": []}
        for merchant in self.merchants:
            self._by_risk[merchant.risk_tier].append(merchant)
        self._high_risk_merchants = [m for m in self.merchants if m.mcc in HIGH_RISK_MCCS] or self.merchants
        self._state: dict[str, _CustomerState] = {}
        self._amount_params = {mcc: (mu, sigma) for mcc, _, _, mu, sigma in MCC_CATALOG}
        # Pending events from a burst, drained before new ones are generated.
        self._pending: deque[dict] = deque()

    # ------------------------------------------------------------------ public API

    def next_event(self, now: datetime | None = None) -> dict:
        """Return the next transaction. A velocity burst queues several events at once;
        they are drained on subsequent calls so the caller's rate limiting still holds."""
        if self._pending:
            return self._pending.popleft()

        now = now or datetime.now(UTC)
        is_fraud = self.rng.random() < self.config.fraud_rate

        if not is_fraud:
            return self._legit_transaction(now)

        anomaly = self.rng.choices(ANOMALY_TYPES, weights=ANOMALY_WEIGHTS, k=1)[0]
        if anomaly == "velocity":
            burst = self._velocity_burst(now)
            self._pending.extend(burst[1:])
            return burst[0]
        if anomaly == "impossible_geo":
            return self._impossible_geo_transaction(now)
        return self._amount_outlier_transaction(now)

    def stream(self, count: int, start: datetime | None = None, step_seconds: float = 0.02):
        """Yield `count` events with synthetic timestamps. Used by tests and `--dry-run`
        so a fixture can be produced without wall-clock waiting."""
        clock = start or datetime.now(UTC)
        for _ in range(count):
            yield self.next_event(clock)
            clock += timedelta(seconds=step_seconds)

    # ------------------------------------------------------------ transaction kinds

    def _legit_transaction(self, now: datetime) -> dict:
        customer = self.rng.choice(self.customers)
        merchant = self._pick_merchant_near(customer)
        amount = self._sample_amount(customer, merchant)
        lat, lon = jitter_location(self.rng, merchant.lat, merchant.lon, radius_km=0.5)
        channel = weighted_choice(self.rng, CHANNELS)[0]
        device_id = self._pick_device(customer, changed=self.rng.random() < self.config.device_change_rate)
        return self._build(customer, merchant, amount, lat, lon, channel, device_id, now, False, None)

    def _velocity_burst(self, now: datetime) -> list[dict]:
        """4–9 transactions for one customer inside ~6 minutes, escalating in amount —
        the classic card-testing-then-cash-out pattern."""
        customer = self.rng.choice(self.customers)
        # A stolen card is used from one compromised device.
        device_id = f"DEV-{self.rng.getrandbits(48):012x}"
        burst_size = self.rng.randint(4, 9)
        events: list[dict] = []
        for i in range(burst_size):
            merchant = self.rng.choice(self._high_risk_merchants)
            base = self._sample_amount(customer, merchant, record=False)
            # Card testing: small probes first, then the real hit.
            escalation = 0.15 if i < burst_size // 2 else 2.5 + i * 0.6
            amount = round(max(1.0, base * escalation), 2)
            lat, lon = jitter_location(self.rng, merchant.lat, merchant.lon, radius_km=1.0)
            channel = weighted_choice(self.rng, FRAUD_CHANNELS)[0]
            ts = now + timedelta(seconds=i * self.rng.randint(20, 70))
            events.append(
                self._build(customer, merchant, amount, lat, lon, channel, device_id, ts, True, "velocity")
            )
        return events

    def _impossible_geo_transaction(self, now: datetime) -> dict:
        """A transaction far offshore, minutes after the customer's last domestic one.
        The implied travel speed is physically impossible — that is the whole signal."""
        customer = self.rng.choice(self.customers)
        state = self._state.get(customer.customer_id)
        if state is None:
            # No prior transaction to contradict — seed one so the pair exists.
            self._seed_state(customer, now - timedelta(minutes=self.rng.randint(3, 25)))
        city, lat, lon = self.rng.choice(OFFSHORE_CITIES)
        merchant = self.rng.choice(self._high_risk_merchants)
        amount = round(self._sample_amount(customer, merchant, record=False) * self.rng.uniform(1.5, 4.0), 2)
        t_lat, t_lon = jitter_location(self.rng, lat, lon, radius_km=15)
        device_id = f"DEV-{self.rng.getrandbits(48):012x}"
        channel = weighted_choice(self.rng, FRAUD_CHANNELS)[0]
        return self._build(
            customer, merchant, amount, t_lat, t_lon, channel, device_id, now, True, "impossible_geo"
        )

    def _amount_outlier_transaction(self, now: datetime) -> dict:
        """A single charge 8–30x the customer's typical spend, at a high-risk merchant."""
        customer = self.rng.choice(self.customers)
        merchant = self.rng.choice(self._high_risk_merchants)
        base = self._sample_amount(customer, merchant, record=False)
        amount = round(base * self.rng.uniform(8.0, 30.0), 2)
        lat, lon = jitter_location(self.rng, merchant.lat, merchant.lon, radius_km=2.0)
        channel = weighted_choice(self.rng, FRAUD_CHANNELS)[0]
        device_id = f"DEV-{self.rng.getrandbits(48):012x}"
        return self._build(
            customer, merchant, amount, lat, lon, channel, device_id, now, True, "amount_outlier"
        )

    # ---------------------------------------------------------------------- helpers

    def _build(
        self,
        customer: Customer,
        merchant: Merchant,
        amount: float,
        lat: float,
        lon: float,
        channel: str,
        device_id: str,
        ts: datetime,
        is_fraud: bool,
        anomaly_type: str | None,
    ) -> dict:
        event = {
            "transaction_id": str(uuid.UUID(int=self.rng.getrandbits(128), version=4)),
            "customer_id": customer.customer_id,
            "merchant_id": merchant.merchant_id,
            "mcc": merchant.mcc,
            "amount": amount,
            "currency": weighted_choice(self.rng, CURRENCIES)[0],
            "timestamp": ts.astimezone(UTC).isoformat(timespec="milliseconds"),
            "lat": lat,
            "lon": lon,
            "device_id": device_id,
            "channel": channel,
            "is_fraud": is_fraud,
            # Operational fields — not part of the business schema.
            "ingest_timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "schema_version": self.config.schema_version,
            # Ground-truth label for the anomaly archetype. Kept so the silver feature
            # logic can be scored against what was actually injected; the model-facing
            # gold layer never reads it.
            "anomaly_type": anomaly_type,
        }
        if self.config.schema_version >= 2:
            # The additional field that drives the Iceberg schema-evolution demo.
            event["auth_response_code"] = self._sample_auth_code(is_fraud)

        self._remember(customer, lat, lon, ts, device_id, amount)
        return event

    def _sample_auth_code(self, is_fraud: bool) -> str:
        if is_fraud and self.rng.random() < 0.35:
            return self.rng.choice(["05", "51", "59"])
        return weighted_choice(self.rng, AUTH_RESPONSE_CODES)[0]

    def _pick_merchant_near(self, customer: Customer) -> Merchant:
        """80% of legitimate spend happens in the customer's home metro. Without this,
        every transaction looks like travel and the geo feature carries no information."""
        if self.rng.random() < 0.8:
            local = [m for m in self.merchants if m.city == customer.home_city]
            if local:
                return self.rng.choice(local)
        return self.rng.choice(self.merchants)

    def _sample_amount(self, customer: Customer, merchant: Merchant, record: bool = True) -> float:
        mu, sigma = self._amount_params.get(merchant.mcc, (3.8, 0.9))
        raw = math.exp(self.rng.gauss(mu, sigma)) * customer.spend_factor
        amount = round(min(max(raw, 1.0), 25_000.0), 2)
        if record:
            state = self._state.get(customer.customer_id)
            if state is not None:
                state.recent_amounts.append(amount)
        return amount

    def _pick_device(self, customer: Customer, changed: bool) -> str:
        if not changed:
            return customer.primary_device_id
        return f"DEV-{self.rng.getrandbits(48):012x}"

    def _seed_state(self, customer: Customer, ts: datetime) -> None:
        self._state[customer.customer_id] = _CustomerState(
            last_lat=customer.home_lat,
            last_lon=customer.home_lon,
            last_ts=ts,
            last_device_id=customer.primary_device_id,
        )

    def _remember(
        self, customer: Customer, lat: float, lon: float, ts: datetime, device_id: str, amount: float
    ) -> None:
        state = self._state.get(customer.customer_id)
        if state is None:
            self._seed_state(customer, ts)
            state = self._state[customer.customer_id]
        state.last_lat, state.last_lon, state.last_ts = lat, lon, ts
        state.last_device_id = device_id
        state.recent_amounts.append(amount)
