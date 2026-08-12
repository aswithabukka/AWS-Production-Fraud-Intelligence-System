"""Adversarial tests for the SQL validator.

The validator is the security boundary between a language model and a query engine, so
these tests are written as attacks, not as examples. Each one is a way a real injection
would try to get past a naive check.

The organising idea: assume the model is fully compromised — that the SQL it emits was
written by an attacker via a prompt-injected document — and confirm the parser still holds.
"""

from __future__ import annotations

import pytest

from agents.sql_validator import (
    DEFAULT_MAX_LIMIT,
    SqlValidationError,
    ValidationResult,
    is_safe,
    validate_sql,
)

GOLD = "fraud_gold.fraud_metrics_daily"
RISK = "fraud_gold.merchant_risk"
SILVER = "fraud_silver.transactions"


def ok(sql: str, **kwargs) -> ValidationResult:
    return validate_sql(sql, **kwargs)


def rejected(sql: str, **kwargs) -> str:
    with pytest.raises(SqlValidationError) as excinfo:
        validate_sql(sql, **kwargs)
    return str(excinfo.value)


# ------------------------------------------------------------------- legitimate SQL


def test_simple_select_is_allowed():
    result = ok(f"SELECT dt, fraud_rate_pct FROM {GOLD} WHERE dt >= DATE '2026-01-01'")
    assert result.tables == [GOLD]
    assert result.limit_applied == DEFAULT_MAX_LIMIT
    assert result.limit_was_injected is True


def test_aggregation_with_group_by_is_allowed():
    result = ok(f"""
        SELECT mcc, sum(fraud_transaction_count) AS fraud_txns
        FROM {GOLD}
        WHERE dt >= current_date - INTERVAL '30' DAY
        GROUP BY mcc
        ORDER BY fraud_txns DESC
    """)
    assert result.tables == [GOLD]


def test_join_between_two_allowlisted_tables_is_allowed():
    result = ok(f"""
        SELECT m.merchant_id, m.fraud_rate_pct, d.dt
        FROM {RISK} m
        JOIN {GOLD} d ON d.mcc = m.mcc
    """)
    assert result.tables == [GOLD, RISK]


def test_cte_over_allowlisted_tables_is_allowed():
    """The period-over-period comparison from the definition of done."""
    result = ok(f"""
        WITH recent AS (
            SELECT mcc, sum(fraud_transaction_count) AS fraud_txns
            FROM {GOLD}
            WHERE dt >= current_date - INTERVAL '30' DAY
            GROUP BY mcc
        ),
        prior AS (
            SELECT mcc, sum(fraud_transaction_count) AS fraud_txns
            FROM {GOLD}
            WHERE dt >= current_date - INTERVAL '60' DAY
              AND dt <  current_date - INTERVAL '30' DAY
            GROUP BY mcc
        )
        SELECT r.mcc, r.fraud_txns, p.fraud_txns AS prior_fraud_txns
        FROM recent r
        LEFT JOIN prior p ON p.mcc = r.mcc
    """)
    # `recent` and `prior` are CTE names, not tables — they must not appear here.
    assert result.tables == [GOLD]


def test_union_of_allowlisted_tables_is_allowed():
    assert is_safe(f"SELECT mcc FROM {GOLD} UNION ALL SELECT mcc FROM {RISK}")


def test_subquery_over_allowlisted_table_is_allowed():
    result = ok(f"SELECT * FROM (SELECT mcc, fraud_rate_pct FROM {GOLD}) t WHERE t.fraud_rate_pct > 1")
    assert result.tables == [GOLD]


# --------------------------------------------------------------- stacked statements


@pytest.mark.parametrize(
    "attack",
    [
        f"SELECT * FROM {GOLD}; DROP TABLE {GOLD}",
        f"SELECT * FROM {GOLD};DELETE FROM {GOLD}",
        f"SELECT * FROM {GOLD}; INSERT INTO {GOLD} VALUES (1)",
        f"SELECT 1 FROM {GOLD}; UPDATE {GOLD} SET fraud_rate_pct = 0",
        f"SELECT * FROM {GOLD};;DROP TABLE {GOLD}",
    ],
)
def test_stacked_statements_are_rejected(attack):
    """The classic. A check that parses only the first statement passes all of these."""
    message = rejected(attack)
    assert "statement" in message.lower()


# ------------------------------------------------------------------- DDL / DML


@pytest.mark.parametrize(
    "attack",
    [
        f"DROP TABLE {GOLD}",
        f"DELETE FROM {GOLD}",
        f"INSERT INTO {GOLD} SELECT * FROM {GOLD}",
        f"UPDATE {GOLD} SET fraud_rate_pct = 0",
        f"CREATE TABLE evil AS SELECT * FROM {GOLD}",
        f"ALTER TABLE {GOLD} ADD COLUMN x int",
        f"MERGE INTO {GOLD} t USING {RISK} s ON t.mcc = s.mcc WHEN MATCHED THEN DELETE",
        f"TRUNCATE TABLE {GOLD}",
        "GRANT SELECT ON fraud_silver.transactions TO analyst",
        f"CREATE VIEW peek AS SELECT * FROM {SILVER}",
    ],
)
def test_write_operations_are_rejected(attack):
    rejected(attack)


def test_ctas_hidden_in_a_cte_is_rejected():
    """A CREATE reached through a WITH clause still has to be a CREATE node in the tree."""
    rejected(f"CREATE TABLE stolen AS WITH x AS (SELECT * FROM {GOLD}) SELECT * FROM x")


# ---------------------------------------------------------------- table allowlist


def test_silver_table_is_rejected():
    """Silver holds per-transaction rows including customer identifiers. The agent gets
    aggregates, not raw transactions."""
    message = rejected(f"SELECT * FROM {SILVER}")
    assert "allowlist" in message.lower()


def test_bronze_table_is_rejected():
    rejected("SELECT * FROM fraud_bronze.transactions")


def test_union_reaching_a_silver_table_is_rejected():
    """The allowlist has to apply to *every* branch, not just the first one — checking
    only the leading SELECT is a real and common bug."""
    message = rejected(f"SELECT mcc FROM {GOLD} UNION ALL SELECT mcc FROM {SILVER}")
    assert SILVER in message


def test_union_reaching_silver_from_the_left_is_rejected():
    rejected(f"SELECT mcc FROM {SILVER} UNION ALL SELECT mcc FROM {GOLD}")


def test_cte_referencing_a_non_allowlisted_table_is_rejected():
    """The CTE body is where the real read happens; the outer SELECT looks innocent."""
    message = rejected(f"""
        WITH leaked AS (SELECT customer_id, amount FROM {SILVER})
        SELECT customer_id, amount FROM leaked
    """)
    assert SILVER in message


def test_nested_cte_referencing_silver_is_rejected():
    rejected(f"""
        WITH a AS (SELECT * FROM {GOLD}),
             b AS (SELECT * FROM {SILVER})
        SELECT * FROM a JOIN b ON a.mcc = b.mcc
    """)


def test_subquery_referencing_silver_is_rejected():
    rejected(f"SELECT * FROM {GOLD} WHERE mcc IN (SELECT mcc FROM {SILVER})")


def test_join_reaching_silver_is_rejected():
    rejected(f"SELECT * FROM {GOLD} g JOIN {SILVER} s ON g.mcc = s.mcc")


def test_cte_named_after_an_allowlisted_table_does_not_launder_a_silver_read():
    """Naming a CTE `fraud_metrics_daily` makes the outer query *look* allowlisted. The
    silver read inside the CTE body must still be caught."""
    rejected(f"""
        WITH fraud_metrics_daily AS (SELECT customer_id FROM {SILVER})
        SELECT * FROM fraud_metrics_daily
    """)


def test_information_schema_is_rejected():
    """Schema enumeration is reconnaissance. The agent introspects through the Glue
    Catalog API with its own scoped permissions, not through SQL."""
    rejected("SELECT * FROM information_schema.tables")


def test_cross_catalog_reference_is_rejected():
    rejected("SELECT * FROM othercatalog.fraud_gold.fraud_metrics_daily")


def test_unqualified_table_defaults_to_the_gold_database():
    """A bare `fraud_metrics_daily` is resolved against the default database rather than
    being waved through as unknown."""
    result = ok("SELECT dt FROM fraud_metrics_daily")
    assert result.tables == [GOLD]


def test_unqualified_non_allowlisted_table_is_rejected():
    rejected("SELECT * FROM some_other_table")


# --------------------------------------------------------------- comment injection


def test_trailing_comment_cannot_hide_a_second_statement():
    """`-- ; DROP` is inert because the parser treats it as a comment, but the test pins
    that behaviour so a future switch to string matching fails loudly."""
    result = ok(f"SELECT dt FROM {GOLD} -- ; DROP TABLE {GOLD}")
    assert result.tables == [GOLD]


def test_block_comment_inside_a_keyword_does_not_smuggle_a_drop():
    """`DR/**/OP` defeats a regex. The parser either reads it as a DROP node — rejected —
    or fails to parse it — also rejected. Either way it does not execute."""
    rejected(f"DR/**/OP TABLE {GOLD}")


def test_comment_cannot_conceal_a_silver_reference():
    rejected(f"SELECT * FROM /* {GOLD} */ {SILVER}")


def test_commented_out_limit_still_gets_a_real_limit():
    result = ok(f"SELECT dt FROM {GOLD} -- LIMIT 5")
    assert result.limit_applied == DEFAULT_MAX_LIMIT
    assert result.limit_was_injected is True


# -------------------------------------------------------------------- limit handling


def test_limit_is_injected_when_absent():
    result = ok(f"SELECT dt FROM {GOLD}")
    assert result.limit_was_injected is True
    assert "LIMIT 1000" in result.sql.upper()


def test_existing_small_limit_is_preserved():
    result = ok(f"SELECT dt FROM {GOLD} LIMIT 10")
    assert result.limit_applied == 10
    assert result.limit_was_injected is False


def test_oversized_limit_is_clamped():
    """An unbounded result set is both an Athena scan-budget problem and a prompt that no
    longer fits the model's context."""
    result = ok(f"SELECT dt FROM {GOLD} LIMIT 5000000")
    assert result.limit_applied == DEFAULT_MAX_LIMIT
    assert "5000000" not in result.sql


def test_custom_max_limit_is_respected():
    result = ok(f"SELECT dt FROM {GOLD}", max_limit=25)
    assert result.limit_applied == 25


# ------------------------------------------------------------------- malformed input


@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
def test_empty_input_is_rejected(bad):
    rejected(bad)


def test_gibberish_is_rejected():
    rejected("this is not sql at all ((((")


def test_statement_with_no_table_is_rejected():
    """A constant-only query is either a probe or something the table walker failed to
    understand. Both fail closed — silence is not evidence of safety."""
    rejected("SELECT 1")


def test_error_messages_name_the_offending_table():
    """The agent feeds the rejection back to the model as a repair hint, so the message
    has to say what was wrong, not just that something was."""
    message = rejected(f"SELECT * FROM {SILVER}")
    assert SILVER in message
    assert "fraud_gold" in message  # tells the model what it may use instead


# --------------------------------------------------------------------- allowlist API


def test_allowlist_is_configurable():
    custom = {"fraud_gold.custom_table"}
    assert is_safe("SELECT a FROM fraud_gold.custom_table", allowed_tables=custom)
    assert not is_safe(f"SELECT a FROM {GOLD}", allowed_tables=custom)


def test_allowlist_matching_is_case_insensitive():
    assert is_safe("SELECT DT FROM FRAUD_GOLD.FRAUD_METRICS_DAILY")


def test_is_safe_never_raises():
    for candidate in ["", "DROP TABLE x", "((((", f"SELECT * FROM {GOLD}"]:
        assert isinstance(is_safe(candidate), bool)
