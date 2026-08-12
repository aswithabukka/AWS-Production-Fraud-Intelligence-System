"""query_lakehouse — natural language to Athena, behind the SQL validator.

    introspect the Glue Catalog for gold-layer schemas
      -> generate SQL with a small, cheap model
      -> validate: single SELECT, allowlisted tables, LIMIT enforced  (agents.sql_validator)
      -> execute on Athena
      -> return rows + the SQL that actually ran

Returning the executed SQL is not a nicety. An analytics answer nobody can check is worth
less than no answer, and the SQL is what makes it checkable — including checking that the
validator's LIMIT injection did not silently truncate the result the question needed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import boto3

from agents.bedrock import BedrockClient, user
from agents.catalog import get_schema_cache
from agents.config import get_config
from agents.sql_validator import SqlValidationError, validate_sql

logger = logging.getLogger(__name__)

QUERY_LAKEHOUSE_SPEC = {
    "toolSpec": {
        "name": "query_lakehouse",
        "description": (
            "Answer quantitative questions about card-transaction fraud by querying the "
            "gold layer of the lakehouse (daily fraud metrics by MCC and channel, and "
            "per-merchant risk). Use for anything involving counts, rates, amounts, "
            "trends, comparisons between periods, or rankings. Returns result rows and "
            "the SQL that was executed."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The analytical question, in natural language.",
                    }
                },
                "required": ["question"],
            }
        },
    }
}

SQL_SYSTEM_PROMPT = """You write Trino/Athena SQL against a fraud analytics lakehouse.

Available tables — you may reference NO others:

{schema}

Rules:
- Emit exactly one SELECT statement. No DDL, no DML, no multiple statements.
- Reference only the tables above. Silver and bronze tables are not available to you.
- Prefer explicit column names over SELECT *.
- Filter on `dt` whenever the question implies a time period — it is the partition key,
  and an unfiltered scan will be rejected by the workgroup's scan limit.
- Use Trino date syntax: `current_date - INTERVAL '30' DAY`, `DATE '2026-01-01'`.
- Column names are self-describing; use them literally. `fraud_rate_pct` is already a
  percentage — do not multiply it by 100 again.
- Return only the SQL. No prose, no markdown fences, no explanation.
"""


def _strip_fences(text: str) -> str:
    """Models wrap SQL in markdown fences roughly half the time regardless of instruction."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        cleaned = cleaned.removeprefix("sql").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rindex("```")]
    return cleaned.strip().rstrip(";").strip()


def generate_sql(question: str, client: BedrockClient | None = None, repair_hint: str = "") -> str:
    config = get_config()
    client = client or BedrockClient(config)
    schema = get_schema_cache().schema_prompt()

    prompt = (
        question
        if not repair_hint
        else (
            f"{question}\n\nYour previous attempt was rejected by the SQL validator:\n"
            f"{repair_hint}\n\nWrite a corrected query that satisfies the rules."
        )
    )

    response = client.converse(
        messages=[user(prompt)],
        system=SQL_SYSTEM_PROMPT.format(schema=schema),
        model_id=config.sql_model_id,
        max_tokens=config.sql_max_tokens,
        temperature=0.0,
    )
    return _strip_fences(response.text)


def run_athena_query(sql: str) -> dict[str, Any]:
    """Execute against Athena and poll to completion.

    The workgroup enforces the 1 GB per-query scan cap, so a runaway query fails here
    rather than billing. `data_scanned_bytes` is returned because it is the number that
    makes the cost story concrete in a demo.
    """
    config = get_config()
    athena = boto3.client("athena", region_name=config.region)

    start_args: dict[str, Any] = {
        "QueryString": sql,
        "WorkGroup": config.athena_workgroup,
        "QueryExecutionContext": {"Database": config.athena_database},
    }
    if config.athena_output_location:
        start_args["ResultConfiguration"] = {"OutputLocation": config.athena_output_location}

    execution_id = athena.start_query_execution(**start_args)["QueryExecutionId"]

    deadline = time.time() + config.athena_timeout_seconds
    delay = 0.25
    while True:
        execution = athena.get_query_execution(QueryExecutionId=execution_id)["QueryExecution"]
        state = execution["Status"]["State"]

        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() > deadline:
            athena.stop_query_execution(QueryExecutionId=execution_id)
            raise TimeoutError(f"Athena query exceeded {config.athena_timeout_seconds}s and was cancelled")

        time.sleep(delay)
        # Back off gently: most gold-layer queries finish in a second or two, so a tight
        # initial poll keeps latency low without hammering the API on the slow ones.
        delay = min(delay * 1.5, 2.0)

    statistics = execution.get("Statistics", {})
    if state != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", "unknown")
        raise RuntimeError(f"Athena query {state}: {reason}")

    rows, columns = _fetch_results(athena, execution_id, config.max_rows_returned)

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "data_scanned_bytes": statistics.get("DataScannedInBytes", 0),
        "execution_time_ms": statistics.get("EngineExecutionTimeInMillis", 0),
        "query_execution_id": execution_id,
    }


def _fetch_results(athena, execution_id: str, max_rows: int) -> tuple[list[dict], list[str]]:
    paginator = athena.get_paginator("get_query_results")
    columns: list[str] = []
    rows: list[dict] = []

    for page in paginator.paginate(QueryExecutionId=execution_id):
        metadata = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
        if not columns:
            columns = [c["Name"] for c in metadata]

        for index, row in enumerate(page["ResultSet"]["Rows"]):
            # Athena repeats the header as the first row of the first page only.
            if not rows and index == 0:
                continue
            values = [field.get("VarCharValue") for field in row["Data"]]
            rows.append(dict(zip(columns, values, strict=False)))

            if len(rows) >= max_rows:
                return rows, columns

    return rows, columns


def query_lakehouse(question: str, max_repair_attempts: int = 1) -> dict[str, Any]:
    """Full NL -> SQL -> validate -> execute path.

    One repair attempt, not an open loop: a validator rejection is fed back to the model
    once as a hint, and a second failure is reported rather than retried. Retrying a model
    that has already misunderstood the constraint mostly buys more tokens.
    """
    client = BedrockClient()
    hint = ""
    attempts: list[dict[str, str]] = []

    for attempt in range(max_repair_attempts + 1):
        raw_sql = generate_sql(question, client=client, repair_hint=hint)

        try:
            validated = validate_sql(
                raw_sql,
                allowed_tables=set(get_config().gold_tables),
                max_limit=get_config().max_rows_returned,
            )
        except SqlValidationError as exc:
            attempts.append({"sql": raw_sql, "rejected_because": str(exc)})
            hint = str(exc)
            logger.warning("SQL validation failed (attempt %s): %s", attempt + 1, exc)
            continue

        try:
            result = run_athena_query(validated.sql)
        except (RuntimeError, TimeoutError) as exc:
            return {
                "status": "error",
                "error": str(exc),
                "sql": validated.sql,
                "rejected_attempts": attempts,
            }

        return {
            "status": "ok",
            "sql": validated.sql,
            "tables_read": validated.tables,
            "limit_was_injected": validated.limit_was_injected,
            "rejected_attempts": attempts,
            "token_usage": client.usage.as_dict(),
            **result,
        }

    return {
        "status": "rejected",
        "error": "generated SQL failed validation and could not be repaired",
        "rejected_attempts": attempts,
        "token_usage": client.usage.as_dict(),
    }
