"""MCP server exposing the lakehouse tools.

The same callables the LangGraph supervisor uses, published over the Model Context
Protocol so any MCP client — Claude Desktop, an IDE, another agent — can call them
directly. One implementation, three transports (graph, MCP, HTTP). Nothing is
reimplemented per surface, which is what keeps `agents.sql_validator` the single
chokepoint it is supposed to be: an MCP client cannot reach Athena around it.

Run locally:

    python -m mcp_server.server

Note on the package name: this directory is `mcp_server/`, not `mcp/`, because a local
package named `mcp` shadows the installed MCP SDK — `from mcp.server import ...` would
resolve to this file and import itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server import MCPServer

from agents.config import get_config
from agents.sql_validator import DEFAULT_MAX_LIMIT, SqlValidationError, validate_sql
from agents.tools import pipeline_status as _pipeline_status
from agents.tools import query_lakehouse as _query_lakehouse
from agents.tools import search_policies as _search_policies

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fraud-lake-mcp")

server = MCPServer(
    name="fraud-lake",
    version="0.1.0",
    instructions=(
        "Tools over a card-transaction fraud lakehouse. Use query_lakehouse for numbers, "
        "search_policies for what the policy documents say, and pipeline_status for whether "
        "the data pipeline is healthy. SQL is generated and validated server-side; the "
        "gold layer is the only data these tools can reach."
    ),
)


@server.tool()
async def query_lakehouse(question: str) -> dict[str, Any]:
    """Answer a quantitative question about card-transaction fraud.

    Generates SQL from the question, validates it (exactly one SELECT, gold-layer tables
    only, LIMIT enforced), executes it on Athena, and returns the rows along with the SQL
    that actually ran and the bytes scanned.

    Args:
        question: The analytical question, in natural language.
    """
    # boto3 is blocking. Calling it directly in an async handler stalls the whole server
    # for the duration of an Athena query.
    return await asyncio.to_thread(_query_lakehouse, question)


@server.tool()
async def search_policies(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the fraud-policy corpus and return passages with source citations.

    Covers chargeback windows, dispute thresholds, liability shift, and merchant
    onboarding requirements.

    Args:
        query: What to look for in the policy corpus.
        max_results: How many passages to return.
    """
    return await asyncio.to_thread(_search_policies, query, max_results)


@server.tool()
async def pipeline_status(max_executions: int = 5) -> dict[str, Any]:
    """Report data-pipeline health.

    Recent Step Functions executions, recent Glue job runs, time since the last success,
    and the cause of any failure.

    Args:
        max_executions: How many recent executions to inspect.
    """
    return await asyncio.to_thread(_pipeline_status, max_executions)


@server.tool()
async def check_sql(sql: str) -> dict[str, Any]:
    """Check SQL against the read-only policy without executing it.

    Exposed so a client can see exactly why a statement would be refused — the same
    validator the agent runs, with no query cost.

    Args:
        sql: The SQL statement to check.
    """
    try:
        validated = validate_sql(
            sql,
            allowed_tables=set(get_config().gold_tables),
            max_limit=DEFAULT_MAX_LIMIT,
        )
    except SqlValidationError as exc:
        return {"status": "rejected", "reason": str(exc)}

    return {
        "status": "ok",
        "sql": validated.sql,
        "tables": validated.tables,
        "limit_applied": validated.limit_applied,
        "limit_was_injected": validated.limit_was_injected,
    }


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
