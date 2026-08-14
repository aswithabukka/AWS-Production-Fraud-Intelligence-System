"""Fraud-scoring ensemble over the silver feature table.

Five models, deliberately diverse in inductive bias, ensembled by averaging:

    LightGBM        gradient boosting — fast, strong tabular baseline
    XGBoost         gradient boosting — different regularisation and tree growth
    RandomForest    bagged trees — variance reduction, robust to feature scaling
    SVM (RBF)       margin-based — a genuinely different decision geometry
    IsolationForest unsupervised anomaly score — needs no labels at all, so it
                    contributes signal even where the label is wrong or missing

Everything in this module is pure pandas/sklearn — no Spark, no AWS — so the whole
training path is unit-testable locally in seconds. The Glue job (`glue/ml_job.py`) is a
thin wrapper: read silver → call these functions → write Iceberg.

Honesty note, also recorded in docs/decisions.md: the silver features were engineered to
catch exactly the fraud archetypes the generator injects, so holdout metrics here
validate the *feature engineering*, not real-world fraud performance. That is the correct
claim for a synthetic portfolio system, and the only defensible one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

RANDOM_SEED = 42

# SVM training is O(n^2)-ish; above this many rows it trains on a stratified subsample.
# The other models always see the full training split.
SVM_MAX_TRAIN_ROWS = 8_000

# Numeric silver features the models consume. Missing values carry meaning here
# ("no prior transaction"), so each gets an explicit indicator instead of silent fill.
NUMERIC_FEATURES = [
    "amount",
    "txn_count_1h",
    "txn_count_24h",
    "amount_sum_1h",
    "amount_sum_24h",
    "distinct_merchants_24h",
    "amount_zscore_30d",
    "prior_txn_count_30d",
    "merchant_risk_score",
    "geo_distance_from_prior_km",
    "implied_speed_kmh",
    "seconds_since_prior_txn",
    "fraud_signal_count",
]

BOOLEAN_FEATURES = ["device_change_flag", "is_high_velocity", "is_amount_outlier", "is_impossible_travel"]

CHANNELS = ["card_present", "ecommerce", "contactless", "recurring"]

LABEL = "is_fraud"


@dataclass
class EnsembleResult:
    scores: pd.DataFrame
    """One row per input transaction: per-model probabilities, the ensemble score,
    and the thresholded prediction."""

    metrics: pd.DataFrame
    """One row per model (plus the ensemble) with holdout AUC / precision / recall / F1."""

    threshold: float
    """Ensemble-score cutoff used for predicted_is_fraud, chosen on the training split."""

    feature_names: list[str] = field(default_factory=list)


def prepare_features(silver: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Silver rows -> (model matrix X, label y).

    All transformations are stateless and row-local, so training-time and scoring-time
    preparation cannot drift apart — the classic train/serve skew trap.
    """
    df = silver.copy()

    X = pd.DataFrame(index=df.index)

    for col in NUMERIC_FEATURES:
        # df.get() returns a scalar NaN for absent columns; build a real Series so an
        # entirely missing feature degrades to (all-missing indicator + zero fill).
        series = (
            pd.to_numeric(df[col], errors="coerce")
            if col in df
            else pd.Series(np.nan, index=df.index, dtype=float)
        )
        # The indicator carries "this value was absent" as signal; the fill keeps the
        # matrix dense. Both, not either: a bare zero-fill would make "no prior
        # transaction" look identical to "prior transaction at distance zero".
        X[f"{col}_missing"] = series.isna().astype(np.int8)
        X[col] = series.fillna(0.0)

    # Implied speed uses a large sentinel for same-second moves; cap so the SVM's
    # scaler is not dominated by one column's outliers.
    X["implied_speed_kmh"] = X["implied_speed_kmh"].clip(upper=100_000.0)
    X["seconds_since_prior_txn"] = X["seconds_since_prior_txn"].clip(upper=86_400.0 * 30)

    for col in BOOLEAN_FEATURES:
        X[col] = df.get(col).fillna(False).astype(np.int8) if col in df else np.int8(0)

    channel = df.get("channel", pd.Series(index=df.index, dtype=object))
    for name in CHANNELS:
        X[f"channel_{name}"] = (channel == name).astype(np.int8)

    y = (
        df[LABEL].fillna(False).astype(np.int8)
        if LABEL in df
        else pd.Series(0, index=df.index, dtype=np.int8)
    )
    return X, y


def _build_models(scale_pos_weight: float) -> dict[str, Any]:
    """The four supervised models. Isolation Forest is handled separately — it never
    sees labels, which is the point of including it."""
    import lightgbm as lgb
    import xgboost as xgb

    return {
        "lightgbm": lgb.LGBMClassifier(
            n_estimators=300,
            learning_rate=0.05,
            num_leaves=31,
            class_weight="balanced",
            random_state=RANDOM_SEED,
            verbose=-1,
        ),
        "xgboost": xgb.XGBClassifier(
            n_estimators=300,
            learning_rate=0.05,
            max_depth=6,
            scale_pos_weight=scale_pos_weight,
            random_state=RANDOM_SEED,
            eval_metric="logloss",
            verbosity=0,
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=300,
            class_weight="balanced",
            n_jobs=-1,
            random_state=RANDOM_SEED,
        ),
        # Scaling matters only to the SVM, so the scaler lives inside its pipeline
        # rather than being applied globally (the trees neither need nor want it).
        "svm": Pipeline(
            [
                ("scale", StandardScaler()),
                (
                    "svc",
                    SVC(
                        kernel="rbf",
                        probability=True,
                        class_weight="balanced",
                        random_state=RANDOM_SEED,
                    ),
                ),
            ]
        ),
    }


def _iforest_scores(
    model: IsolationForest, X: pd.DataFrame, train_min: float, train_max: float
) -> np.ndarray:
    """Map Isolation Forest's decision function into [0, 1] where 1 = most anomalous.

    Normalisation bounds come from the TRAINING split and are clipped at scoring time —
    normalising each batch by its own min/max would make scores incomparable across runs.
    """
    raw = -model.score_samples(X)  # higher = more anomalous
    span = max(train_max - train_min, 1e-9)
    return np.clip((raw - train_min) / span, 0.0, 1.0)


def train_and_score(
    silver: pd.DataFrame,
    test_size: float = 0.25,
    seed: int = RANDOM_SEED,
) -> EnsembleResult:
    """Train all five models, evaluate on a stratified holdout, score every input row.

    Returns scores for the FULL input frame (train + holdout alike — the pipeline wants
    a score on every transaction), while metrics are computed on holdout rows only.
    """
    if len(silver) < 200:
        raise ValueError(f"need at least 200 rows to train, got {len(silver)}")
    X, y = prepare_features(silver)
    if y.nunique() < 2:
        raise ValueError("label has a single class — cannot train supervised models")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, stratify=y, random_state=seed
    )

    pos = max(int(y_train.sum()), 1)
    scale_pos_weight = (len(y_train) - pos) / pos

    models = _build_models(scale_pos_weight)
    proba_full: dict[str, np.ndarray] = {}
    proba_test: dict[str, np.ndarray] = {}

    for name, model in models.items():
        if name == "svm" and len(X_train) > SVM_MAX_TRAIN_ROWS:
            # Stratified subsample: RBF-SVM training cost grows quadratically and the
            # margin geometry stabilises long before the full set is needed.
            sub = X_train.groupby(y_train, group_keys=False).apply(
                lambda g: g.sample(
                    n=max(1, int(SVM_MAX_TRAIN_ROWS * len(g) / len(X_train))), random_state=seed
                )
            )
            model.fit(sub, y_train.loc[sub.index])
        else:
            model.fit(X_train, y_train)

        proba_full[name] = model.predict_proba(X)[:, 1]
        proba_test[name] = model.predict_proba(X_test)[:, 1]

    # Isolation Forest: unsupervised, fit on the training rows' features only.
    iforest = IsolationForest(n_estimators=300, contamination="auto", random_state=seed)
    iforest.fit(X_train)
    raw_train = -iforest.score_samples(X_train)
    lo, hi = float(raw_train.min()), float(raw_train.max())
    proba_full["isolation_forest"] = _iforest_scores(iforest, X, lo, hi)
    proba_test["isolation_forest"] = _iforest_scores(iforest, X_test, lo, hi)

    # ------------------------------------------------------------------ ensemble
    # Equal-weight mean of all five. Simple on purpose: with five diverse members and a
    # portfolio-scale dataset, learned stacking weights would mostly fit noise — and an
    # unweighted mean is trivially explainable in review.
    ensemble_full = np.mean([proba_full[m] for m in proba_full], axis=0)
    ensemble_test = np.mean([proba_test[m] for m in proba_test], axis=0)

    # Threshold chosen on TRAIN (best F1 over a sweep), evaluated on holdout — choosing
    # it on the holdout would quietly leak the test set into the decision rule.
    train_mask = X.index.isin(X_train.index)
    threshold = _best_f1_threshold(y[train_mask].to_numpy(), ensemble_full[train_mask])

    metrics_rows = []
    for name, p in proba_test.items():
        metrics_rows.append(_metric_row(name, y_test.to_numpy(), p, threshold))
    metrics_rows.append(_metric_row("ensemble", y_test.to_numpy(), ensemble_test, threshold))
    metrics = pd.DataFrame(metrics_rows)

    scores = pd.DataFrame(
        {
            "transaction_id": silver["transaction_id"].values,
            "lightgbm_fraud_probability": np.round(proba_full["lightgbm"], 6),
            "xgboost_fraud_probability": np.round(proba_full["xgboost"], 6),
            "random_forest_fraud_probability": np.round(proba_full["random_forest"], 6),
            "svm_fraud_probability": np.round(proba_full["svm"], 6),
            "isolation_forest_anomaly_score": np.round(proba_full["isolation_forest"], 6),
            "ensemble_fraud_score": np.round(ensemble_full, 6),
            "predicted_is_fraud": ensemble_full >= threshold,
            "actual_is_fraud": y.astype(bool).values,
            "in_holdout": ~train_mask,
        }
    )
    if "dt" in silver:
        scores["dt"] = silver["dt"].values

    return EnsembleResult(
        scores=scores,
        metrics=metrics,
        threshold=float(threshold),
        feature_names=list(X.columns),
    )


def _best_f1_threshold(y_true: np.ndarray, scores: np.ndarray) -> float:
    best_t, best_f1 = 0.5, -1.0
    for t in np.linspace(0.05, 0.95, 91):
        f1 = f1_score(y_true, scores >= t, zero_division=0)
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1
    return best_t


def _metric_row(name: str, y_true: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    preds = scores >= threshold
    return {
        "model_name": name,
        "holdout_roc_auc": round(float(roc_auc_score(y_true, scores)), 4),
        "holdout_average_precision": round(float(average_precision_score(y_true, scores)), 4),
        "holdout_precision": round(float(precision_score(y_true, preds, zero_division=0)), 4),
        "holdout_recall": round(float(recall_score(y_true, preds, zero_division=0)), 4),
        "holdout_f1": round(float(f1_score(y_true, preds, zero_division=0)), 4),
        "decision_threshold": round(float(threshold), 4),
    }
