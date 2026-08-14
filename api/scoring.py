"""Real-time scoring: load the persisted ensemble from S3 and score single events.

The models were trained by the Glue job and serialised to `models/<run>/` with a
`latest.json` manifest (model keys, decision threshold, exact feature-column order).
This module loads that bundle once (TTL-cached), so a score is a few milliseconds of
CPU — no retraining, no Spark, no Glue.

The honest caveat, stated where it belongs: velocity/z-score features describe a
customer's HISTORY, which a single incoming event does not carry. Three sources, best
first:

  1. caller-provided features (a stream processor that already computed them),
  2. the DynamoDB online feature store (rolling per-customer state maintained by the
     Kinesis consumer Lambda),
  3. zeros + missing-indicators (the models were trained to treat absence as signal).
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import boto3
import joblib
import numpy as np
import pandas as pd

from agents.config import get_config
from ml.ensemble import prepare_features

logger = logging.getLogger(__name__)

SUPERVISED = ("lightgbm", "xgboost", "random_forest", "svm")


@dataclass
class ModelBundle:
    models: dict[str, Any]
    threshold: float
    feature_names: list[str]
    model_run_id: str
    loaded_at: float = field(default_factory=time.time)


class ScoringService:
    """TTL-cached loader + scorer over the persisted ensemble."""

    def __init__(self, models_uri: str, features_table: str = "", ttl_seconds: int = 600) -> None:
        self.models_uri = models_uri.rstrip("/")
        self.features_table = features_table
        self.ttl = ttl_seconds
        self._bundle: ModelBundle | None = None
        self._lock = threading.Lock()
        self._s3 = boto3.client("s3", region_name=get_config().region)
        self._ddb = (
            boto3.resource("dynamodb", region_name=get_config().region).Table(features_table)
            if features_table
            else None
        )

    # ------------------------------------------------------------------ loading

    def _bucket_key(self, uri: str) -> tuple[str, str]:
        rest = uri.removeprefix("s3://")
        bucket, _, key = rest.partition("/")
        return bucket, key

    def bundle(self) -> ModelBundle:
        with self._lock:
            if self._bundle and (time.time() - self._bundle.loaded_at) < self.ttl:
                return self._bundle

            bucket, prefix = self._bucket_key(self.models_uri)
            manifest = json.loads(
                self._s3.get_object(Bucket=bucket, Key=f"{prefix}/latest.json")["Body"].read()
            )
            models: dict[str, Any] = {}
            for name, key in manifest["models"].items():
                blob = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                models[name] = joblib.load(io.BytesIO(blob))
            self._bundle = ModelBundle(
                models=models,
                threshold=float(manifest["threshold"]),
                feature_names=list(manifest["feature_names"]),
                model_run_id=manifest["model_run_id"],
            )
            logger.info("loaded model bundle %s (%s models)", manifest["model_run_id"], len(models))
            return self._bundle

    # ------------------------------------------------------- online features

    def customer_features(self, customer_id: str) -> dict[str, Any]:
        """Rolling per-customer state from the online feature store, if configured."""
        if self._ddb is None or not customer_id:
            return {}
        try:
            item = self._ddb.get_item(Key={"customer_id": customer_id}).get("Item")
        except Exception as exc:  # noqa: BLE001 - feature store down must degrade, not 500
            logger.warning("feature store read failed: %s", exc)
            return {}
        if not item:
            return {}
        events = item.get("events", [])
        now = time.time()
        ts = [float(e["t"]) for e in events]
        amts = [float(e["a"]) for e in events]
        in_1h = [i for i, t in enumerate(ts) if now - t <= 3600]
        in_24h = [i for i, t in enumerate(ts) if now - t <= 86400]
        feats: dict[str, Any] = {
            "txn_count_1h": len(in_1h) + 1,  # +1: the event being scored counts too
            "txn_count_24h": len(in_24h) + 1,
            "amount_sum_1h": sum(amts[i] for i in in_1h),
            "amount_sum_24h": sum(amts[i] for i in in_24h),
            "prior_txn_count_30d": len(events),
            "seconds_since_prior_txn": (now - max(ts)) if ts else None,
        }
        if len(amts) >= 3:
            mean = float(np.mean(amts))
            std = float(np.std(amts, ddof=1))
            feats["_amount_mean_30d"] = mean
            feats["_amount_std_30d"] = std if std > 0 else None
        return feats

    # ------------------------------------------------------------------ scoring

    def score(self, event: dict[str, Any]) -> dict[str, Any]:
        bundle = self.bundle()
        started = time.perf_counter()

        # Layer the three feature sources: zeros ← feature store ← caller-provided.
        feats: dict[str, Any] = {"transaction_id": event.get("transaction_id", "adhoc")}
        store = self.customer_features(str(event.get("customer_id", "")))
        feats.update({k: v for k, v in store.items() if not k.startswith("_")})
        feats.update({k: v for k, v in event.items() if v is not None})

        if "amount_zscore_30d" not in feats and store.get("_amount_std_30d"):
            feats["amount_zscore_30d"] = (float(event.get("amount", 0)) - store["_amount_mean_30d"]) / store[
                "_amount_std_30d"
            ]

        # In-distribution proxies for features silver ALWAYS populates. The training
        # data never has these missing, so a missing-indicator at serve time is
        # out-of-distribution and sends tree models into never-trained leaves (found
        # live: an alarming event scored 0.29 because half its features carried
        # indicators the models had never seen set). Approximations mirror silver's own
        # fallbacks — merchant risk defaults to silver's unknown-merchant prior.
        amount = float(feats.get("amount", 0) or 0)
        n1h = float(feats.get("txn_count_1h", 1) or 1)
        n24h = float(feats.get("txn_count_24h", n1h) or n1h)
        feats.setdefault("txn_count_1h", int(n1h))
        feats.setdefault("txn_count_24h", int(max(n24h, n1h)))
        feats.setdefault("amount_sum_1h", amount * n1h)
        feats.setdefault("amount_sum_24h", amount * max(n24h, n1h))
        feats.setdefault("distinct_merchants_24h", max(1, int(n24h // 2)))
        feats.setdefault("merchant_risk_score", 0.02)
        zscore_given = feats.get("amount_zscore_30d") is not None
        feats.setdefault("prior_txn_count_30d", 5 if zscore_given else 0)
        # geo_distance / implied_speed / seconds_since_prior stay absent when unknown:
        # silver genuinely has NULLs there (first transactions), so their
        # missing-indicators ARE in-distribution.

        # Derive the composite flags EXACTLY as the silver job does — train/serve
        # parity for derived features. Without this, the models see "no signals fired"
        # regardless of how alarming the raw inputs are (found live: a 7-txn/hour,
        # z-score-6 event scored 0.29 because these defaulted to zero).
        feats.setdefault(
            "is_high_velocity",
            float(feats.get("txn_count_1h", 0) or 0) >= 5 or float(feats.get("txn_count_24h", 0) or 0) >= 20,
        )
        feats.setdefault("is_amount_outlier", abs(float(feats.get("amount_zscore_30d", 0) or 0)) >= 3.0)
        feats.setdefault("is_impossible_travel", float(feats.get("implied_speed_kmh", 0) or 0) > 900.0)
        feats.setdefault(
            "fraud_signal_count",
            int(feats["is_high_velocity"])
            + int(feats["is_amount_outlier"])
            + int(feats["is_impossible_travel"])
            + int(bool(feats.get("device_change_flag"))),
        )

        frame = pd.DataFrame([feats])
        X, _ = prepare_features(frame)
        # The manifest's column order is the contract — training and serving must agree.
        X = X.reindex(columns=bundle.feature_names, fill_value=0)

        per_model: dict[str, float] = {}
        for name in SUPERVISED:
            per_model[name] = float(bundle.models[name].predict_proba(X)[0, 1])
        iforest = bundle.models["isolation_forest"]
        per_model["isolation_forest"] = float(np.clip(-iforest.score_samples(X)[0], 0, 1))
        per_model["autoencoder"] = float(bundle.models["autoencoder"].scores(X)[0])

        ensemble = float(np.mean(list(per_model.values())))

        # Why: LightGBM native contributions, top 3 positive.
        contrib = bundle.models["lightgbm"].predict(X, pred_contrib=True)[0, :-1]
        top = np.argsort(-contrib)[:3]
        factors = [bundle.feature_names[i] for i in top if contrib[i] > 0]

        return {
            "ensemble_fraud_score": round(ensemble, 6),
            "predicted_is_fraud": ensemble >= bundle.threshold,
            "decision_threshold": bundle.threshold,
            "model_scores": {k: round(v, 6) for k, v in per_model.items()},
            "top_risk_factors": factors,
            "features_from_store": sorted(k for k in store if not k.startswith("_")),
            "model_run_id": bundle.model_run_id,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


_service: ScoringService | None = None


def get_scoring_service() -> ScoringService:
    global _service
    if _service is None:
        import os

        _service = ScoringService(
            models_uri=os.environ.get("MODELS_URI", "s3://fraud-lake-434661699277/models"),
            features_table=os.environ.get("FEATURES_TABLE", ""),
        )
    return _service
