"""Tests for the ML ensemble — pure pandas/sklearn, no AWS, no Spark.

The fixture builds a dataset where fraud is *learnable by construction* (fraud rows get
systematically higher velocity/z-score/speed), so a healthy training path must clear a
meaningful AUC bar. If these fail, the ensemble broke — not the data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm")
pytest.importorskip("xgboost")

from ml.ensemble import (  # noqa: E402
    LABEL,
    EnsembleResult,
    prepare_features,
    train_and_score,
)

RNG = np.random.default_rng(7)
N = 3_000
FRAUD_RATE = 0.06


@pytest.fixture(scope="module")
def silver_frame() -> pd.DataFrame:
    """Synthetic silver-shaped rows with informative features."""
    is_fraud = RNG.random(N) < FRAUD_RATE
    lift = np.where(is_fraud, 1.0, 0.0)

    df = pd.DataFrame(
        {
            "transaction_id": [f"T{i:06d}" for i in range(N)],
            "dt": pd.to_datetime("2026-08-14").date(),
            "amount": np.round(np.exp(RNG.normal(3.8 + 1.6 * lift, 0.8)), 2),
            "txn_count_1h": (1 + RNG.poisson(0.4 + 4.0 * lift)).astype(int),
            "txn_count_24h": (1 + RNG.poisson(2.0 + 6.0 * lift)).astype(int),
            "amount_sum_1h": np.round(np.exp(RNG.normal(4.0 + 1.5 * lift, 0.9)), 2),
            "amount_sum_24h": np.round(np.exp(RNG.normal(4.8 + 1.4 * lift, 0.9)), 2),
            "distinct_merchants_24h": (1 + RNG.poisson(1.0 + 2.5 * lift)).astype(int),
            "amount_zscore_30d": RNG.normal(0.0 + 3.5 * lift, 1.0),
            "prior_txn_count_30d": RNG.integers(0, 25, N),
            "merchant_risk_score": np.clip(RNG.normal(0.03 + 0.05 * lift, 0.02), 0, 1),
            "geo_distance_from_prior_km": np.abs(RNG.normal(20 + 3000 * lift, 40)),
            "implied_speed_kmh": np.abs(RNG.normal(30 + 5000 * lift, 60)),
            "seconds_since_prior_txn": np.abs(RNG.normal(20000 - 15000 * lift, 8000)),
            "fraud_signal_count": np.minimum(4, RNG.poisson(0.2 + 2.0 * lift)).astype(int),
            "device_change_flag": RNG.random(N) < (0.05 + 0.5 * lift),
            "is_high_velocity": RNG.random(N) < (0.03 + 0.6 * lift),
            "is_amount_outlier": RNG.random(N) < (0.02 + 0.5 * lift),
            "is_impossible_travel": RNG.random(N) < (0.01 + 0.4 * lift),
            "channel": RNG.choice(["card_present", "ecommerce", "contactless", "recurring"], N),
            LABEL: is_fraud,
        }
    )
    # Realistic missingness: first transactions have no geo/zscore history.
    first_txn = RNG.random(N) < 0.12
    df.loc[first_txn, ["geo_distance_from_prior_km", "implied_speed_kmh", "seconds_since_prior_txn"]] = np.nan
    df.loc[RNG.random(N) < 0.35, "amount_zscore_30d"] = np.nan
    return df


@pytest.fixture(scope="module")
def result(silver_frame) -> EnsembleResult:
    return train_and_score(silver_frame)


# ---------------------------------------------------------------- feature prep


def test_feature_matrix_is_fully_numeric_and_dense(silver_frame):
    X, y = prepare_features(silver_frame)
    assert not X.isna().any().any(), "model matrix must be dense"
    assert X.select_dtypes(exclude=[np.number]).empty
    assert len(y) == len(silver_frame)


def test_missingness_becomes_signal_not_silence(silver_frame):
    X, _ = prepare_features(silver_frame)
    assert X["geo_distance_from_prior_km_missing"].sum() > 0
    # The indicator must match where the raw value was actually absent.
    raw_missing = silver_frame["geo_distance_from_prior_km"].isna()
    assert (X["geo_distance_from_prior_km_missing"].astype(bool) == raw_missing.values).all()


def test_prepare_features_handles_absent_columns():
    minimal = pd.DataFrame({"transaction_id": ["a"] * 300, "amount": 10.0, LABEL: [True, False] * 150})
    X, y = prepare_features(minimal)
    assert not X.isna().any().any()
    assert y.sum() == 150


# ---------------------------------------------------------------- training path


def test_all_five_models_produce_scores(result):
    for col in (
        "lightgbm_fraud_probability",
        "xgboost_fraud_probability",
        "random_forest_fraud_probability",
        "svm_fraud_probability",
        "isolation_forest_anomaly_score",
        "ensemble_fraud_score",
    ):
        assert col in result.scores.columns
        assert result.scores[col].between(0, 1).all(), f"{col} must live in [0, 1]"


def test_every_input_row_is_scored(result, silver_frame):
    assert len(result.scores) == len(silver_frame)
    assert result.scores["transaction_id"].is_unique


def test_metrics_cover_all_models_plus_ensemble(result):
    assert set(result.metrics["model_name"]) == {
        "lightgbm",
        "xgboost",
        "random_forest",
        "svm",
        "isolation_forest",
        "ensemble",
    }


def test_supervised_models_actually_learn(result):
    """The fixture makes fraud learnable by construction; failing this bar means the
    training path is broken, not the data."""
    supervised = result.metrics[
        result.metrics.model_name.isin(["lightgbm", "xgboost", "random_forest", "svm"])
    ]
    assert (supervised["holdout_roc_auc"] > 0.85).all(), supervised.to_string()


def test_ensemble_is_competitive_with_members(result):
    by_model = result.metrics.set_index("model_name")["holdout_roc_auc"]
    # The unsupervised member drags the mean by design; the ensemble must still land
    # within a whisker of the best single model.
    assert by_model["ensemble"] >= by_model.drop("ensemble").max() - 0.05


def test_isolation_forest_carries_signal_without_labels(result):
    """Weaker than the supervised models, necessarily — but clearly better than chance,
    or including it in the ensemble is pure noise."""
    auc = result.metrics.set_index("model_name").loc["isolation_forest", "holdout_roc_auc"]
    assert auc > 0.60


def test_threshold_is_sane_and_predictions_follow_it(result):
    assert 0.05 <= result.threshold <= 0.95
    implied = result.scores["ensemble_fraud_score"] >= result.threshold
    assert (implied == result.scores["predicted_is_fraud"]).all()


def test_deterministic_across_runs(silver_frame):
    a = train_and_score(silver_frame)
    b = train_and_score(silver_frame)
    pd.testing.assert_series_equal(a.scores["ensemble_fraud_score"], b.scores["ensemble_fraud_score"])
    assert a.threshold == b.threshold


def test_refuses_degenerate_inputs(silver_frame):
    with pytest.raises(ValueError, match="at least 200"):
        train_and_score(silver_frame.head(50))

    single_class = silver_frame.copy()
    single_class[LABEL] = False
    with pytest.raises(ValueError, match="single class"):
        train_and_score(single_class)
