"""Fraud-scoring ensemble over the silver feature table.

Five models, deliberately diverse in inductive bias, ensembled by averaging:

    LightGBM        gradient boosting — fast, strong tabular baseline
    XGBoost         gradient boosting — different regularisation and tree growth
    RandomForest    bagged trees — variance reduction, robust to feature scaling
    SVM (RBF)       margin-based — a genuinely different decision geometry
    IsolationForest unsupervised anomaly score — needs no labels at all
    Autoencoder     bottleneck MLP trained to reconstruct LEGITIMATE rows only;
                    fraud reconstructs poorly, so reconstruction error is the score.
                    The two unsupervised members fail differently: iForest measures
                    isolatability, the autoencoder distance from the normal manifold —
                    so the ensemble keeps signal even where the label is wrong or missing

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
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.neural_network import MLPRegressor
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

    fitted_models: dict = field(default_factory=dict, repr=False)
    """The trained estimators, keyed by model name — persisted to S3 by the job so a
    future real-time scorer can load them without retraining."""


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


class _Autoencoder:
    """Bottleneck MLP autoencoder for anomaly scoring, in plain sklearn.

    MLPRegressor learns X -> X through a narrow middle layer. Trained ONLY on
    legitimate rows: the network learns the manifold of normal behaviour, and rows far
    from it — fraud — come back with high reconstruction error. Inputs are standardised
    (reconstruction MSE is meaningless across unscaled feature magnitudes), and the
    error->score normalisation uses TRAINING quantiles so scores are comparable across
    runs; robust quantiles beat min/max because one extreme training row would
    otherwise flatten everyone else's score.
    """

    def __init__(self, seed: int) -> None:
        self.scaler = StandardScaler()
        self.net = MLPRegressor(
            hidden_layer_sizes=(32, 8, 32),  # the 8-unit bottleneck is the whole idea
            activation="relu",
            max_iter=400,
            early_stopping=True,
            random_state=seed,
        )
        self.err_lo = 0.0
        self.err_hi = 1.0

    def _errors(self, X: pd.DataFrame) -> np.ndarray:
        Z = self.scaler.transform(X)
        recon = self.net.predict(Z)
        return np.mean((Z - recon) ** 2, axis=1)

    def fit(self, X_legit: pd.DataFrame) -> _Autoencoder:
        import warnings

        from sklearn.exceptions import ConvergenceWarning

        Z = self.scaler.fit_transform(X_legit)
        with warnings.catch_warnings():
            # early_stopping governs reconstruction quality; max_iter is a cost bound.
            # Hitting it is expected on some datasets and not a defect worth a log line.
            warnings.simplefilter("ignore", ConvergenceWarning)
            self.net.fit(Z, Z)
        train_err = self._errors(X_legit)
        self.err_lo = float(np.quantile(train_err, 0.01))
        self.err_hi = float(np.quantile(train_err, 0.99))
        return self

    def scores(self, X: pd.DataFrame) -> np.ndarray:
        span = max(self.err_hi - self.err_lo, 1e-9)
        return np.clip((self._errors(X) - self.err_lo) / span, 0.0, 1.0)


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
    split: str = "random",
    false_positive_cost: float | None = None,
    false_negative_cost: float | None = None,
) -> EnsembleResult:
    """Train all models, evaluate on a holdout, score every input row.

    `split="temporal"` trains on the past and tests on the future (ordered by
    transaction time) — the methodologically honest evaluation for fraud, since random
    splits let the model peek at the future of the very customers it is tested on.
    Falls back to random when no usable time column exists.

    If both costs are given, the decision threshold minimises expected cost
    (missed_fraud × fn_cost + false_alarm × fp_cost) on the TRAINING split instead of
    maximising F1 — which is how real fraud teams set thresholds, because a missed
    $2,000 fraud and a 30-second analyst review are not symmetric mistakes.
    """
    if len(silver) < 200:
        raise ValueError(f"need at least 200 rows to train, got {len(silver)}")
    X, y = prepare_features(silver)
    if y.nunique() < 2:
        raise ValueError("label has a single class — cannot train supervised models")

    time_col = next(
        (c for c in ("transaction_timestamp", "dt") if c in silver and silver[c].nunique() > 1), None
    )
    if split == "temporal" and time_col is not None:
        order = silver[time_col].argsort(kind="stable")
        cut = int(len(order) * (1 - test_size))
        train_idx, test_idx = X.index[order[:cut]], X.index[order[cut:]]
        X_train, X_test = X.loc[train_idx], X.loc[test_idx]
        y_train, y_test = y.loc[train_idx], y.loc[test_idx]
        if y_train.nunique() < 2 or y_test.nunique() < 2:
            # Degenerate period (e.g. no fraud yet in the early window) — fall back.
            X_train, X_test, y_train, y_test = train_test_split(
                X, y, test_size=test_size, stratify=y, random_state=seed
            )
    else:
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

    # Autoencoder: trained on LEGITIMATE training rows only, so the label's only role
    # is selecting the normal manifold — it never supervises the reconstruction.
    autoencoder = _Autoencoder(seed).fit(X_train[y_train.to_numpy() == 0])
    proba_full["autoencoder"] = autoencoder.scores(X)
    proba_test["autoencoder"] = autoencoder.scores(X_test)

    # ------------------------------------------------------------------ ensemble
    # Equal-weight mean of all six. Simple on purpose: with six diverse members and a
    # portfolio-scale dataset, learned stacking weights would mostly fit noise — and an
    # unweighted mean is trivially explainable in review.
    ensemble_full = np.mean([proba_full[m] for m in proba_full], axis=0)
    ensemble_test = np.mean([proba_test[m] for m in proba_test], axis=0)

    # Threshold chosen on TRAIN (best F1 over a sweep), evaluated on holdout — choosing
    # it on the holdout would quietly leak the test set into the decision rule.
    train_mask = X.index.isin(X_train.index)
    if false_positive_cost is not None and false_negative_cost is not None:
        threshold = _min_cost_threshold(
            y[train_mask].to_numpy(),
            ensemble_full[train_mask],
            false_positive_cost,
            false_negative_cost,
        )
    else:
        threshold = _best_f1_threshold(y[train_mask].to_numpy(), ensemble_full[train_mask])

    # 5-fold cross-validated AUC on the TRAINING split for the fast supervised models —
    # evidence that performance is stable across splits, not a lucky holdout. The SVM is
    # excluded on cost grounds (5 refits of an O(n^2) kernel), and CV-AUC is not
    # meaningful for the unsupervised members; those report null and the holdout column
    # remains their evidence.
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=seed)
    cv_stats: dict[str, tuple[float, float]] = {}
    for name in ("lightgbm", "xgboost", "random_forest"):
        folds = cross_val_score(models[name], X_train, y_train, cv=cv, scoring="roc_auc", n_jobs=-1)
        cv_stats[name] = (round(float(folds.mean()), 4), round(float(folds.std()), 4))

    metrics_rows = []
    for name, p in proba_test.items():
        row = _metric_row(name, y_test.to_numpy(), p, threshold)
        mean_std = cv_stats.get(name)
        row["cv5_auc_mean"] = mean_std[0] if mean_std else None
        row["cv5_auc_std"] = mean_std[1] if mean_std else None
        metrics_rows.append(row)
    ens_row = _metric_row("ensemble", y_test.to_numpy(), ensemble_test, threshold)
    ens_row["cv5_auc_mean"] = None
    ens_row["cv5_auc_std"] = None
    metrics_rows.append(ens_row)
    metrics = pd.DataFrame(metrics_rows)

    # Per-row top risk factors from LightGBM's native prediction contributions
    # (pred_contrib — SHAP-style attributions with zero extra dependencies). The three
    # most score-raising features, human-readable: the "why" next to every score.
    contrib = models["lightgbm"].predict(X, pred_contrib=True)[:, :-1]  # drop bias term
    top3_idx = np.argsort(-contrib, axis=1)[:, :3]
    feature_arr = np.array(X.columns)
    top_factors = [
        ", ".join(f"{feature_arr[j]}" for j in row if contrib[i, j] > 0) or "none"
        for i, row in enumerate(top3_idx)
    ]

    scores = pd.DataFrame(
        {
            "transaction_id": silver["transaction_id"].values,
            "lightgbm_fraud_probability": np.round(proba_full["lightgbm"], 6),
            "xgboost_fraud_probability": np.round(proba_full["xgboost"], 6),
            "random_forest_fraud_probability": np.round(proba_full["random_forest"], 6),
            "svm_fraud_probability": np.round(proba_full["svm"], 6),
            "isolation_forest_anomaly_score": np.round(proba_full["isolation_forest"], 6),
            "autoencoder_reconstruction_score": np.round(proba_full["autoencoder"], 6),
            "ensemble_fraud_score": np.round(ensemble_full, 6),
            "predicted_is_fraud": ensemble_full >= threshold,
            "top_risk_factors": top_factors,
            "actual_is_fraud": y.astype(bool).values,
            "in_holdout": ~train_mask,
        }
    )
    if "dt" in silver:
        scores["dt"] = silver["dt"].values

    fitted = dict(models)
    fitted["isolation_forest"] = iforest
    fitted["autoencoder"] = autoencoder

    return EnsembleResult(
        scores=scores,
        metrics=metrics,
        threshold=float(threshold),
        feature_names=list(X.columns),
        fitted_models=fitted,
    )


def _min_cost_threshold(y_true: np.ndarray, scores: np.ndarray, fp_cost: float, fn_cost: float) -> float:
    """Threshold minimising expected business cost on the training split."""
    best_t, best_cost = 0.5, float("inf")
    for t in np.linspace(0.05, 0.95, 91):
        preds = scores >= t
        cost = fp_cost * float(((preds == 1) & (y_true == 0)).sum()) + fn_cost * float(
            ((preds == 0) & (y_true == 1)).sum()
        )
        if cost < best_cost:
            best_t, best_cost = float(t), cost
    return best_t


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
