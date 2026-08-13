"""Glue Data Quality rulesets, expressed in DQDL.

Two rulesets, one per layer, because bronze and silver answer different questions:

  bronze  - "is this data structurally trustworthy?"  completeness, uniqueness, ranges,
            referential integrity, freshness
  silver  - "did the feature computation actually work?"  a feature column that is 100%
            NULL passes every bronze rule and is still completely broken

Thresholds live next to the bronze validator's constants in `glue.transforms` so the two
cannot drift. A DQ rule that contradicts the ingest validator produces a permanently
failing gate nobody can explain.
"""

from __future__ import annotations

from glue.transforms import MAX_AMOUNT, MIN_AMOUNT, VALID_CHANNELS

# How stale the newest record may be before the pipeline is considered stalled. Matches
# the 3-hour CloudWatch freshness alarm in slice 1c — one number, two enforcement points.
FRESHNESS_HOURS = 3

_CHANNEL_LIST = ", ".join(f'"{c}"' for c in VALID_CHANNELS)


def bronze_ruleset(
    merchant_dim_table: str = "fraud_silver.merchant_dim",
    include_referential: bool = True,
) -> str:
    """Structural integrity of the bronze table.

    `merchant_dim_table` is fully qualified because ReferentialIntegrity resolves it
    through the Glue Catalog, not the current database.

    `include_referential=False` is the cold-start mode: the merchant dimension is
    *created by the silver job*, which runs after this gate — so on the very first
    pipeline run the referenced table cannot exist yet. The quality job detects that
    and drops the one rule rather than failing the whole pipeline on a bootstrapping
    paradox. From the second run onward the rule is always enforced.
    """
    referential_rule = (
        f"""
    # Every merchant_id in bronze must exist in the merchant dimension. This is the rule
    # the corrupted-batch exercise trips with its unknown_merchant records.
    ReferentialIntegrity "merchant_id" "{merchant_dim_table}.{{merchant_id}}" = 1.0,
"""
        if include_referential
        else ""
    )
    return f"""
Rules = [{referential_rule}
    # Nothing downstream can identify a transaction without these three.
    IsComplete "transaction_id",
    IsComplete "customer_id",
    IsComplete "merchant_id",

    # The dedupe in the bronze job is what makes this hold. If this rule ever fails,
    # the MERGE key or the ordering in dedupe_transactions is wrong — it is a direct
    # test of the duplicate-replay guarantee.
    IsUnique "transaction_id",

    IsComplete "transaction_timestamp",
    IsComplete "amount",
    ColumnValues "amount" between {MIN_AMOUNT} and {MAX_AMOUNT},

    ColumnValues "channel" in [{_CHANNEL_LIST}],
    ColumnValues "lat" between -90 and 90,
    ColumnValues "lon" between -180 and 180,

    # Currency is allowed to be sparse in a way the identifiers are not.
    Completeness "currency" > 0.99,

    # The pipeline is stalled if the newest record is older than this.
    ColumnValues "transaction_timestamp" > (now() - {FRESHNESS_HOURS} hours),

    # An empty run is a failure, not a success. Without this rule a broken upstream
    # produces a green pipeline over zero rows — the worst possible outcome, because
    # nothing alerts.
    RowCount > 0
]
"""


def silver_ruleset() -> str:
    """Did the feature computation produce usable features?"""
    return """
Rules = [
    IsComplete "transaction_id",
    IsUnique "transaction_id",

    # Velocity counters are counts over a window that always contains at least the
    # current row, so 1 is the floor. A 0 here means the window spec is broken.
    ColumnValues "txn_count_1h" >= 1,
    ColumnValues "txn_count_24h" >= 1,
    IsComplete "txn_count_1h",
    IsComplete "txn_count_24h",

    # The z-score is deliberately NULL for customers with fewer than 3 prior
    # transactions, so it is never fully complete — but if it is almost never populated
    # the 30-day window is not matching any history.
    Completeness "amount_zscore_30d" > 0.30,

    # Risk score is a probability. Outside [0, 1] means the smoothing maths is wrong.
    ColumnValues "merchant_risk_score" between 0 and 1,
    IsComplete "merchant_risk_score",
    ColumnValues "merchant_risk_tier" in ["low", "medium", "high", "unknown"],

    # Geo distance is NULL for a customer's first transaction and non-negative after.
    ColumnValues "geo_distance_from_prior_km" >= 0 with threshold >= 0.95,

    IsComplete "fraud_signal_count",
    ColumnValues "fraud_signal_count" between 0 and 4,

    # The composite signal must fire sometimes and must not fire always. Either extreme
    # means the thresholds are wrong and the feature carries no information.
    ColumnValues "is_high_velocity" in ["true", "false"],

    RowCount > 0
]
"""


def gold_ruleset() -> str:
    """Sanity of the aggregates the agent will answer questions from.

    Wrong numbers here are worse than a failed pipeline: the agent will state them
    confidently.
    """
    return """
Rules = [
    IsComplete "dt",
    IsComplete "transaction_count",
    ColumnValues "transaction_count" > 0,

    # Percentages must be percentages. A fraud_rate_pct of 4500 means a division is
    # inverted, and the agent would report it verbatim.
    ColumnValues "fraud_rate_pct" between 0 and 100,
    ColumnValues "approval_rate_pct" between 0 and 100,

    ColumnValues "fraud_loss_amount_usd" >= 0,
    ColumnValues "total_amount_usd" >= 0,

    # A grain that is not unique means the GROUP BY is wrong and every number in the
    # table is double-counted.
    IsUnique "dt" "mcc" "channel",

    RowCount > 0
]
"""


RULESETS: dict[str, str] = {
    "bronze": bronze_ruleset(),
    "silver": silver_ruleset(),
    "gold": gold_ruleset(),
}
