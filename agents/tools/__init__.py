"""The three tool agents, plus their Bedrock tool specifications.

Each tool is a plain callable taking and returning JSON-serialisable dicts, so the same
implementation backs the LangGraph supervisor, the MCP server, and the FastAPI service
without adaptation. One implementation, three transports.
"""

from agents.tools.pipeline_tool import PIPELINE_STATUS_SPEC, pipeline_status
from agents.tools.policy_tool import SEARCH_POLICIES_SPEC, search_policies
from agents.tools.sql_tool import QUERY_LAKEHOUSE_SPEC, query_lakehouse

TOOL_SPECS = [QUERY_LAKEHOUSE_SPEC, SEARCH_POLICIES_SPEC, PIPELINE_STATUS_SPEC]

TOOL_REGISTRY = {
    "query_lakehouse": query_lakehouse,
    "search_policies": search_policies,
    "pipeline_status": pipeline_status,
}

__all__ = [
    "PIPELINE_STATUS_SPEC",
    "QUERY_LAKEHOUSE_SPEC",
    "SEARCH_POLICIES_SPEC",
    "TOOL_REGISTRY",
    "TOOL_SPECS",
    "pipeline_status",
    "query_lakehouse",
    "search_policies",
]
