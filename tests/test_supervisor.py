"""Tests for the supervisor graph's control flow.

No AWS calls: the Bedrock client is a stub returning scripted router decisions, and the
tools are replaced in the registry. What is under test is the *loop* — routing, the
iteration cap, tool-failure handling, and result accumulation — which is the part that
determines whether this system is safe to point at a paid API.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")

from agents.bedrock import ModelResponse, TokenUsage  # noqa: E402
from agents.config import AgentConfig  # noqa: E402
from agents.supervisor import _parse_route, build_supervisor  # noqa: E402
from agents.tools import TOOL_REGISTRY  # noqa: E402


class FakeBedrockClient:
    """Returns scripted responses in order; repeats the last one forever.

    Repeating rather than raising on exhaustion is deliberate — it lets a test script a
    router that *always* asks for another tool, which is exactly the runaway case the
    iteration cap exists to stop.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict] = []
        self.usage = TokenUsage()

    def converse(self, messages, system=None, model_id=None, max_tokens=None, **kwargs) -> ModelResponse:
        index = min(len(self.calls), len(self.responses) - 1)
        text = self.responses[index]
        self.calls.append({"model_id": model_id, "system": system, "messages": messages})
        usage = {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150}
        self.usage.add(model_id or "fake", usage, 12.0)
        return ModelResponse(text=text, stop_reason="end_turn", usage=usage, latency_ms=12.0)


def route(tool: str, **tool_input) -> str:
    return json.dumps({"tool": tool, "input": tool_input, "reasoning": "test"})


@pytest.fixture
def config() -> AgentConfig:
    return AgentConfig(max_iterations=3)


@pytest.fixture
def stub_tools(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def make(name, payload):
        def tool(**kwargs):
            calls.append((name, kwargs))
            return payload

        return tool

    monkeypatch.setitem(
        TOOL_REGISTRY,
        "query_lakehouse",
        make("query_lakehouse", {"status": "ok", "row_count": 2, "sql": "SELECT 1", "rows": [{"a": 1}]}),
    )
    monkeypatch.setitem(
        TOOL_REGISTRY,
        "search_policies",
        make(
            "search_policies",
            {
                "status": "ok",
                "result_count": 1,
                "results": [{"text": "t", "source": "s", "score": 0.9, "document": "policy.pdf"}],
            },
        ),
    )
    monkeypatch.setitem(
        TOOL_REGISTRY,
        "pipeline_status",
        make("pipeline_status", {"status": "ok", "summary": {"succeeded": 1}, "executions": []}),
    )
    return calls


# ---------------------------------------------------------------------- route parsing


def test_router_parses_plain_json():
    decision = _parse_route('{"tool": "query_lakehouse", "input": {"question": "x"}, "reasoning": "r"}')
    assert decision["tool"] == "query_lakehouse"
    assert decision["input"] == {"question": "x"}


def test_router_parses_json_inside_markdown_fences():
    """Models emit fenced JSON regardless of instruction, roughly half the time."""
    decision = _parse_route('```json\n{"tool": "pipeline_status", "input": {}}\n```')
    assert decision["tool"] == "pipeline_status"


def test_router_parses_json_surrounded_by_prose():
    decision = _parse_route('Sure! {"tool": "search_policies", "input": {"query": "q"}} Hope that helps.')
    assert decision["tool"] == "search_policies"


@pytest.mark.parametrize("garbage", ["not json at all", "", "{{{{", "{'tool': 'x'"])
def test_unparseable_routing_falls_through_to_none(garbage):
    """Falling through to synthesis costs one cheap call and yields an honest answer.
    Guessing a tool costs a full iteration and a full tool invocation."""
    assert _parse_route(garbage)["tool"] == "none"


def test_unknown_tool_name_is_not_dispatched():
    """A hallucinated tool name must never reach the registry lookup."""
    decision = _parse_route('{"tool": "rm_minus_rf", "input": {}}')
    assert decision["tool"] == "none"
    assert "unknown tool" in decision["reasoning"]


# ------------------------------------------------------------------------ the loop


def test_single_tool_then_answer(config, stub_tools):
    client = FakeBedrockClient(
        [route("query_lakehouse", question="fraud rate?"), route("none"), "The fraud rate is 1.5%."]
    )
    graph = build_supervisor(client=client, config=config)

    final = graph.invoke({"question": "fraud rate?", "iterations": 0})

    assert final["iterations"] == 1
    assert final["answer"] == "The fraud rate is 1.5%."
    assert [c[0] for c in stub_tools] == ["query_lakehouse"]


def test_no_tool_needed_goes_straight_to_synthesis(config, stub_tools):
    client = FakeBedrockClient([route("none"), "I have nothing to look up."])
    graph = build_supervisor(client=client, config=config)

    final = graph.invoke({"question": "hello", "iterations": 0})

    assert final["iterations"] == 0
    assert stub_tools == []
    # With no tool results the graph short-circuits to the canned response rather than
    # paying for a synthesis call over an empty context.
    assert "could not gather any information" in final["answer"]


def test_multiple_tools_accumulate(config, stub_tools):
    client = FakeBedrockClient(
        [
            route("query_lakehouse", question="q"),
            route("search_policies", query="p"),
            route("none"),
            "Combined answer.",
        ]
    )
    graph = build_supervisor(client=client, config=config)

    final = graph.invoke({"question": "data vs policy", "iterations": 0})

    assert final["iterations"] == 2
    assert [entry["tool"] for entry in final["tool_results"]] == ["query_lakehouse", "search_policies"]
    assert final["answer"] == "Combined answer."


def test_max_iterations_stops_a_runaway_router(config, stub_tools):
    """The failure mode that bills: a router that always wants one more tool call.

    The cap is enforced in the routing function, so the loop degrades to an answer
    instead of raising a recursion error at the user.
    """
    client = FakeBedrockClient([route("query_lakehouse", question="again")])
    graph = build_supervisor(client=client, config=config)

    final = graph.invoke({"question": "unanswerable", "iterations": 0})

    assert final["iterations"] == config.max_iterations == 3
    assert "max_iterations" in final["stopped_reason"]
    assert len(stub_tools) == 3, "the tool must not be called more times than the cap"
    assert final["answer"], "a partial answer is returned rather than an exception"


def test_iteration_cap_is_checked_before_the_model_call(config, stub_tools):
    """Hitting the ceiling costs nothing at all — no extra round trip to Bedrock."""
    client = FakeBedrockClient([route("query_lakehouse", question="again")])
    graph = build_supervisor(client=client, config=config)
    graph.invoke({"question": "q", "iterations": 0})

    # 3 routing calls that dispatched + 1 that hit the cap and returned early + 1
    # synthesis. The capped turn must not have produced a router call.
    router_calls = [c for c in client.calls if c["system"] and "route analytics questions" in c["system"]]
    assert len(router_calls) == config.max_iterations


def test_tool_exception_does_not_kill_the_graph(config, monkeypatch):
    def exploding_tool(**_kwargs):
        raise RuntimeError("Athena is having a day")

    monkeypatch.setitem(TOOL_REGISTRY, "query_lakehouse", exploding_tool)

    client = FakeBedrockClient([route("query_lakehouse", question="q"), route("none"), "Partial answer."])
    graph = build_supervisor(client=client, config=config)

    final = graph.invoke({"question": "q", "iterations": 0})

    assert final["answer"] == "Partial answer."
    assert final["tool_results"][0]["result"]["status"] == "error"
    assert "Athena is having a day" in final["tool_results"][0]["result"]["error"]


def test_bad_tool_arguments_are_reported_not_raised(config, monkeypatch):
    """The router produced arguments the tool does not accept — recoverable, because the
    next routing turn sees the error and can adjust."""
    monkeypatch.setitem(TOOL_REGISTRY, "pipeline_status", lambda max_executions=5: {"status": "ok"})

    client = FakeBedrockClient([route("pipeline_status", nonexistent_arg=1), route("none"), "Answer."])
    graph = build_supervisor(client=client, config=config)

    final = graph.invoke({"question": "did it run", "iterations": 0})
    assert final["tool_results"][0]["result"]["status"] == "error"
    assert "invalid arguments" in final["tool_results"][0]["result"]["error"]


# ------------------------------------------------------------------- model selection


def test_routing_and_synthesis_use_different_models(config, stub_tools):
    """Two tiers on purpose: a cheap model for structured routing, the larger one only
    for the final prose. Using one large model everywhere is the easiest way to grow a
    Bedrock bill without improving answers."""
    client = FakeBedrockClient([route("query_lakehouse", question="q"), route("none"), "Answer."])
    graph = build_supervisor(client=client, config=config)
    graph.invoke({"question": "q", "iterations": 0})

    models_used = [c["model_id"] for c in client.calls]
    assert models_used[0] == config.routing_model_id
    assert models_used[-1] == config.synthesis_model_id
    assert config.routing_model_id != config.synthesis_model_id


def test_trace_records_every_node(config, stub_tools):
    """The trace is what makes a wrong answer debuggable after the fact."""
    client = FakeBedrockClient([route("query_lakehouse", question="q"), route("none"), "Answer."])
    graph = build_supervisor(client=client, config=config)

    final = graph.invoke({"question": "q", "iterations": 0})
    nodes = [entry["node"] for entry in final["trace"]]

    assert "route_query" in nodes
    assert "call_tool" in nodes
    assert "synthesize" in nodes
