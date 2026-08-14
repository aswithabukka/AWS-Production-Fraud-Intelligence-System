"""LangGraph supervisor over the three tool agents.

    route_query  -- decide which tool (if any) the question needs
        |
        v
    call_tool    -- execute it, append the result to state
        |
        +--> back to route_query  (conditional edge)
        |
        v
    synthesize   -- turn accumulated tool results into an answer

**MAX_ITERATIONS is enforced inside `route_query`, not as a graph edge.** A recursion
limit on the graph raises an exception; a check in the routing logic degrades gracefully —
it stops asking for tools and synthesises the best answer available from what it already
has. The user gets a partial answer with a note, rather than a stack trace. That
distinction is the entire reason the loop is written explicitly rather than handed to a
managed agent runtime.

Explicit graph wiring over Bedrock Agents is the same argument: when someone asks "what
happens if the model keeps calling tools", the answer should be a line of code.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.bedrock import BedrockClient, user
from agents.config import AgentConfig, get_config
from agents.tools import TOOL_REGISTRY
from agents.tools.policy_tool import format_citations

logger = logging.getLogger(__name__)


def _append(existing: list, new: list) -> list:
    return (existing or []) + (new or [])


class SupervisorState(TypedDict, total=False):
    question: str
    iterations: int
    next_tool: str
    tool_input: dict[str, Any]
    tool_results: Annotated[list[dict[str, Any]], _append]
    answer: str
    stopped_reason: str
    trace: Annotated[list[dict[str, Any]], _append]


ROUTER_SYSTEM = """You route analytics questions about a card-transaction fraud platform
to exactly one tool, or decide that enough information has been gathered to answer.

Tools:
- query_lakehouse: quantitative questions about the transaction data — counts, rates,
  amounts, trends, comparisons between time periods, rankings by merchant or MCC.
- search_policies: what the fraud policy documents say or require — chargeback windows,
  dispute thresholds, liability rules, onboarding requirements.
- pipeline_status: the health of the data pipeline — did it run, did it succeed, when did
  it last complete, why did it fail.
- none: the information already gathered is sufficient to answer, or no tool can help.

Reply with a JSON object and nothing else:
{"tool": "<tool name or none>", "input": {...}, "reasoning": "<one sentence>"}

For query_lakehouse and search_policies the input is {"question": "..."} and
{"query": "..."} respectively. For pipeline_status the input may be {}.

A question can need more than one tool across turns — for example, comparing what the
data shows against what policy requires. Ask for one tool at a time. When the accumulated
results answer the question, reply with "none"."""

SYNTHESIS_SYSTEM = """You answer questions about a card-transaction fraud analytics platform.

Ground every claim in the tool results provided. Rules:
- Quantitative claims come from query results. Quote the numbers as returned; do not
  recompute or round away significant digits.
- Policy claims cite their source with the [n] markers given in the passages.
- If the tool results do not answer the question, say so plainly and say what is missing.
  Never fill a gap with a plausible number — a wrong figure stated confidently is the
  worst possible output of this system.
- When SQL was executed, mention what it measured so the reader can sanity-check the
  framing.
- Be concise. An analyst is reading this, not a search engine."""


def _parse_route(text: str) -> dict[str, Any]:
    """Parse the router's JSON, tolerating markdown fences and surrounding prose.

    Falls back to `none` rather than guessing a tool: routing to the wrong tool spends a
    full iteration and a full tool call, whereas falling through to synthesis costs one
    cheap model call and produces an honest "I could not determine that".
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].removeprefix("json").strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rindex("```")]

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return {"tool": "none", "input": {}, "reasoning": "router returned no JSON object"}

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {"tool": "none", "input": {}, "reasoning": "router returned malformed JSON"}

    tool = parsed.get("tool", "none")
    if tool not in TOOL_REGISTRY and tool != "none":
        return {"tool": "none", "input": {}, "reasoning": f"router named an unknown tool: {tool}"}

    return {
        "tool": tool,
        "input": parsed.get("input") or {},
        "reasoning": parsed.get("reasoning", ""),
    }


def build_supervisor(
    client: BedrockClient | None = None,
    config: AgentConfig | None = None,
    checkpointer=None,
):
    """Compile the graph. `client` is injectable so the routing logic can be tested
    without Bedrock credentials. Pass a LangGraph checkpointer to enable multi-turn
    conversation memory keyed by thread_id."""
    config = config or get_config()
    client = client or BedrockClient(config)

    # ------------------------------------------------------------------ route_query

    def route_query(state: SupervisorState) -> SupervisorState:
        iterations = state.get("iterations", 0)

        # THE hard stop. Checked before any model call, so hitting the ceiling costs
        # nothing at all rather than one more round trip.
        if iterations >= config.max_iterations:
            logger.warning("MAX_ITERATIONS (%s) reached; forcing synthesis", config.max_iterations)
            return {
                "next_tool": "none",
                "stopped_reason": f"max_iterations ({config.max_iterations}) reached",
                "trace": [{"node": "route_query", "decision": "none", "reason": "iteration cap"}],
            }

        context = _format_results_for_router(state.get("tool_results", []))
        prompt = (
            state["question"]
            if not context
            else (f"Question: {state['question']}\n\nInformation gathered so far:\n{context}")
        )

        response = client.converse(
            messages=[user(prompt)],
            system=ROUTER_SYSTEM,
            model_id=config.routing_model_id,
            max_tokens=config.routing_max_tokens,
            temperature=0.0,
        )
        decision = _parse_route(response.text)

        return {
            "next_tool": decision["tool"],
            "tool_input": decision["input"],
            "trace": [
                {
                    "node": "route_query",
                    "iteration": iterations,
                    "decision": decision["tool"],
                    "reasoning": decision["reasoning"],
                    "latency_ms": round(response.latency_ms, 1),
                }
            ],
        }

    # -------------------------------------------------------------------- call_tool

    def call_tool(state: SupervisorState) -> SupervisorState:
        name = state["next_tool"]
        tool_input = state.get("tool_input") or {}
        started = time.perf_counter()

        try:
            result = TOOL_REGISTRY[name](**tool_input)
        except TypeError as exc:
            # The router produced arguments the tool does not accept. Recoverable: the
            # error goes into state and the next routing turn can adjust.
            result = {"status": "error", "error": f"invalid arguments for {name}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a tool failure must not kill the graph
            logger.exception("tool %s failed", name)
            result = {"status": "error", "error": str(exc)}

        latency_ms = (time.perf_counter() - started) * 1000

        return {
            "iterations": state.get("iterations", 0) + 1,
            "tool_results": [{"tool": name, "input": tool_input, "result": result}],
            "trace": [{"node": "call_tool", "tool": name, "latency_ms": round(latency_ms, 1)}],
        }

    # ------------------------------------------------------------------- synthesize

    def synthesize(state: SupervisorState) -> SupervisorState:
        results = state.get("tool_results", [])
        stopped_reason = state.get("stopped_reason", "")

        if not results:
            return {
                "answer": (
                    "I could not gather any information to answer that. The question does not "
                    "map to the transaction data, the policy corpus, or the pipeline status."
                ),
                "trace": [{"node": "synthesize", "note": "no tool results"}],
            }

        context = _format_results_for_synthesis(results)
        caveat = (
            f"\n\nNote: the tool-call limit was reached ({stopped_reason}). Answer from what "
            "is available and say explicitly what could not be checked."
            if stopped_reason
            else ""
        )

        response = client.converse(
            messages=[user(f"Question: {state['question']}\n\nTool results:\n{context}{caveat}")],
            system=SYNTHESIS_SYSTEM,
            # The one place the larger model earns its cost: turning rows and passages
            # into something a human reads.
            model_id=config.synthesis_model_id,
            max_tokens=config.synthesis_max_tokens,
            temperature=0.2,
        )

        return {
            "answer": response.text,
            "trace": [{"node": "synthesize", "latency_ms": round(response.latency_ms, 1)}],
        }

    # ------------------------------------------------------------- conditional edge

    def should_continue(state: SupervisorState) -> Literal["call_tool", "synthesize"]:
        if state.get("next_tool", "none") == "none":
            return "synthesize"
        if state.get("iterations", 0) >= config.max_iterations:
            # Belt and braces. route_query already enforces this; repeating it here means
            # no future edit to the router can accidentally uncap the loop.
            return "synthesize"
        return "call_tool"

    graph = StateGraph(SupervisorState)
    graph.add_node("route_query", route_query)
    graph.add_node("call_tool", call_tool)
    graph.add_node("synthesize", synthesize)

    graph.add_edge(START, "route_query")
    graph.add_conditional_edges("route_query", should_continue, ["call_tool", "synthesize"])
    graph.add_edge("call_tool", "route_query")
    graph.add_edge("synthesize", END)

    return graph.compile(checkpointer=checkpointer)


# ------------------------------------------------------------------------ formatting


def _format_results_for_router(results: list[dict[str, Any]]) -> str:
    """A compact digest for the router — it needs to know what has been answered, not the
    full payload. Sending complete result sets to the router on every turn is a large and
    entirely avoidable token cost."""
    if not results:
        return ""

    lines = []
    for entry in results:
        result = entry["result"]
        status = result.get("status", "ok")
        if entry["tool"] == "query_lakehouse":
            lines.append(f"- query_lakehouse ({status}): {result.get('row_count', 0)} rows returned")
        elif entry["tool"] == "search_policies":
            lines.append(f"- search_policies ({status}): {result.get('result_count', 0)} passages")
        else:
            summary = result.get("summary", {})
            lines.append(f"- pipeline_status ({status}): {summary.get('succeeded', 0)} recent successes")
    return "\n".join(lines)


def _format_results_for_synthesis(results: list[dict[str, Any]]) -> str:
    """The full payload, formatted per tool. Rows are truncated — 200 rows of JSON is
    thousands of tokens and the model only needs enough to characterise the answer."""
    blocks = []

    for entry in results:
        tool, result = entry["tool"], entry["result"]

        if tool == "query_lakehouse":
            if result.get("status") != "ok":
                blocks.append(f"query_lakehouse FAILED: {result.get('error')}")
                continue
            rows = result.get("rows", [])
            shown = rows[:50]
            blocks.append(
                f"query_lakehouse\nSQL executed:\n{result.get('sql')}\n"
                f"rows returned: {result.get('row_count')} "
                f"(showing {len(shown)}), scanned {result.get('data_scanned_bytes', 0)} bytes\n"
                f"{json.dumps(shown, indent=2, default=str)}"
            )

        elif tool == "search_policies":
            if result.get("status") != "ok":
                blocks.append(f"search_policies unavailable: {result.get('error')}")
                continue
            blocks.append(f"search_policies\n{format_citations(result.get('results', []))}")

        else:
            blocks.append(f"pipeline_status\n{json.dumps(result, indent=2, default=str)}")

    return "\n\n---\n\n".join(blocks)


_MEMORY = None
_MEMORY_GRAPHS: dict[int, Any] = {}


def _conversational_graph(client: BedrockClient):
    """A process-lifetime checkpointer so follow-up questions ("and compared to last
    week?") carry prior tool results as context, keyed by conversation_id. In-memory by
    design: conversation history is ephemeral UX state, not data — losing it on restart
    is correct, cheap, and private."""
    global _MEMORY
    if _MEMORY is None:
        from langgraph.checkpoint.memory import MemorySaver

        _MEMORY = MemorySaver()
    return build_supervisor(client=client, checkpointer=_MEMORY)


def ask(
    question: str,
    client: BedrockClient | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Convenience entry point used by the API and the MCP server."""
    client = client or BedrockClient()
    if conversation_id:
        graph = _conversational_graph(client)
        invoke_config = {"configurable": {"thread_id": conversation_id}}
    else:
        graph = build_supervisor(client=client)
        invoke_config = None

    started = time.perf_counter()
    final = graph.invoke(
        {"question": question, "iterations": 0},
        config=invoke_config,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000

    return {
        "question": question,
        "answer": final.get("answer", ""),
        "iterations": final.get("iterations", 0),
        "stopped_reason": final.get("stopped_reason"),
        "tools_used": [entry["tool"] for entry in final.get("tool_results", [])],
        "sql_executed": [
            entry["result"].get("sql")
            for entry in final.get("tool_results", [])
            if entry["tool"] == "query_lakehouse" and entry["result"].get("sql")
        ],
        "citations": [
            item
            for entry in final.get("tool_results", [])
            if entry["tool"] == "search_policies"
            for item in entry["result"].get("results", [])
        ],
        "trace": final.get("trace", []),
        "latency_ms": round(elapsed_ms, 1),
        "token_usage": client.usage.as_dict(),
    }
