"""Label feedback: merge confirmed ground-truth labels into the training frame.

The production story this models: scores go out in near-real-time, but TRUTH arrives
late — chargebacks and dispute outcomes confirm fraud days or weeks after the
transaction. Feeding those confirmations back is how retraining tracks new fraud
patterns instead of freezing at whatever the first model learned.

Feedback records are tiny JSON lines landed under `feedback/` in the lake:

    {"transaction_id": "...", "confirmed_is_fraud": true, "confirmed_at": "..."}

`apply_feedback` overrides the label for any transaction with a confirmation, keeps
everything else untouched, and reports how many labels changed — the number that tells
you whether the loop is actually feeding the model anything new.
"""

from __future__ import annotations

import pandas as pd


def apply_feedback(silver: pd.DataFrame, feedback: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    """Return (frame with confirmed labels applied, stats about what changed)."""
    stats = {"feedback_rows": 0, "labels_confirmed": 0, "labels_changed": 0}
    if feedback is None or len(feedback) == 0:
        return silver, stats

    fb = feedback.dropna(subset=["transaction_id", "confirmed_is_fraud"]).copy()
    # Last confirmation wins when the same transaction is confirmed twice.
    if "confirmed_at" in fb:
        fb = fb.sort_values("confirmed_at")
    fb = fb.drop_duplicates("transaction_id", keep="last")
    stats["feedback_rows"] = len(fb)

    merged = silver.merge(fb[["transaction_id", "confirmed_is_fraud"]], on="transaction_id", how="left")
    has_confirmation = merged["confirmed_is_fraud"].notna()
    stats["labels_confirmed"] = int(has_confirmation.sum())
    stats["labels_changed"] = int(
        (has_confirmation & (merged["confirmed_is_fraud"].astype("boolean") != merged["is_fraud"])).sum()
    )

    merged.loc[has_confirmation, "is_fraud"] = merged.loc[has_confirmation, "confirmed_is_fraud"].astype(bool)
    return merged.drop(columns=["confirmed_is_fraud"]), stats
