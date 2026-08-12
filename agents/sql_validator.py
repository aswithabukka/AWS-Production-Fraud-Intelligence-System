"""Parser-level SQL validation for anything the model generates.

This is a security control, not a prompt instruction. The model is untrusted input: it can
be steered by a user message, by retrieved document content, or simply be wrong. Every
statement it produces is parsed with `sqlglot` into an AST and checked structurally.

**Regex was never an option.** A regex that blocks `DROP` blocks the string "DROP", not the
operation — `/*x*/DROP`, `DR/**/OP`, `dRoP`, and a `DROP` reached through a CTE all defeat
it, and it simultaneously rejects the legitimate column `dropped_transactions`. Parsing is
the only approach where the check and the thing being checked are the same object.

Four rules, in order:

1. **Exactly one statement.** Stacked statements are rejected before anything else, so
   `SELECT 1; DROP TABLE x` cannot smuggle a second operation past a check that only
   looked at the first.
2. **Read-only.** The root node must be a SELECT (or a set operation over SELECTs). Any
   DDL/DML node anywhere in the tree is fatal.
3. **Table allowlist.** Every table reference — including inside CTEs, subqueries, joins,
   and set operations — must be in the gold-layer allowlist. CTE *names* are resolved and
   excluded so a CTE cannot be mistaken for a table.
4. **Bounded result.** A LIMIT is injected if absent and lowered if it exceeds the cap.

Failing closed is deliberate throughout: anything that cannot be parsed, or that produces
a node type this module does not recognise, is rejected rather than passed through.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECT = "athena"

DEFAULT_ALLOWED_TABLES = frozenset(
    {
        "fraud_gold.fraud_metrics_daily",
        "fraud_gold.merchant_risk",
    }
)

DEFAULT_MAX_LIMIT = 1_000

# Node types that mutate data or schema. The root-must-be-SELECT rule already blocks these
# at the top level; this catches them nested inside a subquery or a CTE body, which is
# where an injection would actually try to hide one.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.Grant,
    exp.TruncateTable,
    exp.Command,  # sqlglot's catch-all for statements it does not model, e.g. GRANT, SET
)

# Set operations are read-only, so they are allowed as a root — but only when every branch
# is itself a SELECT, and the table allowlist still applies to each branch. This is what
# stops `SELECT ... FROM gold UNION ALL SELECT ... FROM silver`.
SET_OPERATIONS: tuple[type[exp.Expression], ...] = (exp.Union, exp.Except, exp.Intersect)


class SqlValidationError(Exception):
    """Raised when generated SQL fails validation. The message is safe to surface."""


@dataclass
class ValidationResult:
    sql: str
    """The validated statement, with LIMIT injected or clamped."""

    tables: list[str] = field(default_factory=list)
    """Fully-qualified tables the statement reads."""

    limit_applied: int | None = None
    """The LIMIT now in force."""

    limit_was_injected: bool = False
    """True when the model omitted a LIMIT and one was added."""


def validate_sql(
    sql: str,
    allowed_tables: frozenset[str] | set[str] = DEFAULT_ALLOWED_TABLES,
    max_limit: int = DEFAULT_MAX_LIMIT,
    default_database: str = "fraud_gold",
) -> ValidationResult:
    """Validate and normalise a generated SQL statement.

    Returns a `ValidationResult` on success; raises `SqlValidationError` on any violation.
    """
    if not sql or not sql.strip():
        raise SqlValidationError("empty statement")

    allowed = {t.lower() for t in allowed_tables}

    # ---------------------------------------------------------------- 1. one statement
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except Exception as exc:  # sqlglot raises several parse error types
        # Fail closed. Unparseable SQL is not "probably fine" — it is SQL this module
        # cannot reason about, which is exactly when it must say no.
        raise SqlValidationError(f"could not parse SQL: {exc}") from exc

    if not statements:
        raise SqlValidationError("no statement found")
    if len(statements) > 1:
        raise SqlValidationError(
            f"expected exactly 1 statement, found {len(statements)} — stacked statements are rejected"
        )

    statement = statements[0]

    # ------------------------------------------------------------------- 2. read-only
    _reject_forbidden_nodes(statement)

    root = statement
    if isinstance(root, exp.Subquery):
        root = root.this

    if not isinstance(root, (exp.Select, *SET_OPERATIONS)):
        raise SqlValidationError(f"only SELECT statements are permitted, got {type(root).__name__.upper()}")

    if isinstance(root, SET_OPERATIONS):
        _assert_set_operation_is_all_selects(root)

    # -------------------------------------------------------------- 3. table allowlist
    tables = _referenced_tables(statement, default_database)
    disallowed = sorted(t for t in tables if t not in allowed)
    if disallowed:
        raise SqlValidationError(
            "query references tables outside the gold-layer allowlist: "
            + ", ".join(disallowed)
            + f" (allowed: {', '.join(sorted(allowed))})"
        )

    if not tables:
        # A query touching no table is either a constant probe or something the table
        # walker failed to understand. Both are rejected — silence is not evidence.
        raise SqlValidationError("query references no allowlisted table")

    # ---------------------------------------------------------------- 4. bounded result
    validated, limit_value, injected = _apply_limit(root, max_limit)

    return ValidationResult(
        sql=validated,
        tables=sorted(tables),
        limit_applied=limit_value,
        limit_was_injected=injected,
    )


# ---------------------------------------------------------------------------- helpers


def _reject_forbidden_nodes(statement: exp.Expression) -> None:
    for node_type in FORBIDDEN_NODES:
        node = statement.find(node_type)
        if node is not None:
            raise SqlValidationError(
                f"{node_type.__name__.upper()} is not permitted — this interface is read-only"
            )


def _assert_set_operation_is_all_selects(node: exp.Expression) -> None:
    """Every branch of a UNION/EXCEPT/INTERSECT must itself be a SELECT or another set
    operation. Nesting is walked rather than assumed to be two levels deep."""
    for side in (node.this, node.expression):
        current = side
        if isinstance(current, exp.Subquery):
            current = current.this
        if isinstance(current, SET_OPERATIONS):
            _assert_set_operation_is_all_selects(current)
        elif not isinstance(current, exp.Select):
            raise SqlValidationError(
                f"set operation branch must be a SELECT, got {type(current).__name__.upper()}"
            )


def _cte_names(statement: exp.Expression) -> set[str]:
    """Names bound by WITH clauses anywhere in the tree.

    These look exactly like table references at the point of use, so without resolving
    them a legitimate CTE would be rejected as an unknown table — and, more importantly,
    a CTE could otherwise be *named* after an allowlisted table to mask a real one.
    """
    names: set[str] = set()
    for cte in statement.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def _referenced_tables(statement: exp.Expression, default_database: str) -> set[str]:
    """Every real table the statement reads, fully qualified and lowercased."""
    cte_names = _cte_names(statement)
    tables: set[str] = set()

    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            continue

        database = (table.db or "").lower()
        catalog = (table.catalog or "").lower()

        # A bare name matching a CTE is a reference to that CTE, not to a table.
        if not database and name in cte_names:
            continue

        qualified = f"{database or default_database.lower()}.{name}"

        # A three-part name reaches into another catalog. The allowlist is expressed in
        # two parts, so anything with an explicit catalog is rejected rather than being
        # silently truncated to something that happens to match.
        if catalog and catalog not in ("awsdatacatalog", "glue_catalog"):
            raise SqlValidationError(f"cross-catalog reference is not permitted: {catalog}.{qualified}")

        tables.add(qualified)

    return tables


def _apply_limit(root: exp.Expression, max_limit: int) -> tuple[str, int, bool]:
    """Ensure the statement returns a bounded number of rows.

    Guards the Athena scan budget and the model's context window at once: an unbounded
    result set is both a cost problem and a prompt that no longer fits.
    """
    existing = root.args.get("limit")
    injected = False

    if existing is None:
        root.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
        limit_value = max_limit
        injected = True
    else:
        try:
            limit_value = int(existing.expression.name)
        except (AttributeError, ValueError):
            # A non-literal LIMIT (a parameter or an expression) cannot be reasoned about,
            # so it is replaced with the cap rather than trusted.
            root.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
            return root.sql(dialect=DIALECT, pretty=True), max_limit, True

        if limit_value > max_limit:
            root.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
            limit_value = max_limit

    return root.sql(dialect=DIALECT, pretty=True), limit_value, injected


def is_safe(sql: str, **kwargs) -> bool:
    """Boolean convenience wrapper. Prefer `validate_sql` — the error message explains
    *why*, and the agent feeds that back to the model as a repair hint."""
    try:
        validate_sql(sql, **kwargs)
        return True
    except SqlValidationError:
        return False
