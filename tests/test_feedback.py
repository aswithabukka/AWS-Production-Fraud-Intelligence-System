"""Tests for the label feedback merge — pure pandas."""

from __future__ import annotations

import pandas as pd

from ml.feedback import apply_feedback


def frame():
    return pd.DataFrame(
        {
            "transaction_id": ["a", "b", "c", "d"],
            "is_fraud": [False, False, True, True],
            "amount": [10.0, 20.0, 30.0, 40.0],
        }
    )


def test_no_feedback_is_a_clean_passthrough():
    out, stats = apply_feedback(frame(), None)
    pd.testing.assert_frame_equal(out, frame())
    assert stats == {"feedback_rows": 0, "labels_confirmed": 0, "labels_changed": 0}


def test_confirmations_override_labels_and_count_changes():
    fb = pd.DataFrame(
        {
            "transaction_id": ["a", "c"],
            "confirmed_is_fraud": [True, True],  # a: changed, c: confirmed same
            "confirmed_at": ["t1", "t1"],
        }
    )
    out, stats = apply_feedback(frame(), fb)
    assert out.set_index("transaction_id")["is_fraud"]["a"] == True  # noqa: E712
    assert stats["labels_confirmed"] == 2
    assert stats["labels_changed"] == 1  # only 'a' actually flipped


def test_latest_confirmation_wins():
    fb = pd.DataFrame(
        {
            "transaction_id": ["b", "b"],
            "confirmed_is_fraud": [True, False],
            "confirmed_at": ["t1", "t2"],  # later says False
        }
    )
    out, _ = apply_feedback(frame(), fb)
    assert out.set_index("transaction_id")["is_fraud"]["b"] == False  # noqa: E712


def test_unknown_transactions_in_feedback_are_ignored():
    fb = pd.DataFrame(
        {
            "transaction_id": ["zzz"],
            "confirmed_is_fraud": [True],
            "confirmed_at": ["t1"],
        }
    )
    out, stats = apply_feedback(frame(), fb)
    assert len(out) == 4
    assert stats["labels_confirmed"] == 0
