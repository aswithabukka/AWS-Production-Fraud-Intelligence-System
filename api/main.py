"""FastAPI service exposing the supervisor graph.

Endpoints:
    POST /ask         run the graph, return the answer with SQL, citations, and telemetry
    POST /ask/stream  the same, streamed as server-sent events
    POST /sql/check   validate SQL without executing it
    GET  /health      liveness + dependency probe for the ALB / ECS health check
    GET  /metrics     process-local counters

Every invocation publishes token usage, latency, and tool-call counts to CloudWatch as
custom metrics. That is what makes the agent layer observable in the same dashboard as
the pipeline, which is the point of the exercise — an agent you cannot measure is an
agent you cannot operate.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from agents.bedrock import BedrockClient
from agents.config import get_config
from agents.sql_validator import SqlValidationError, validate_sql
from agents.supervisor import ask, build_supervisor
from api.models import (
    AskRequest,
    AskResponse,
    HealthResponse,
    SqlCheckRequest,
    SqlCheckResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
logger = logging.getLogger("fraud-lake-api")

VERSION = "0.1.0"

_counters: Counter[str] = Counter()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config = get_config()
    logger.info(
        "starting fraud-lake api | region=%s workgroup=%s max_iterations=%s guardrail=%s",
        config.region,
        config.athena_workgroup,
        config.max_iterations,
        "on" if config.guardrail_id else "off",
    )
    yield
    logger.info("shutting down")


app = FastAPI(
    title="fraud-lake agent API",
    description="Agentic analytics over a card-transaction fraud lakehouse.",
    version=VERSION,
    lifespan=lifespan,
)


_CONSOLE = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    """The ops console — a single self-contained HTML file, no build step.

    Served by the API itself so the UI ships inside the same container: one image, one
    port, nothing extra to deploy or keep in sync.
    """
    return FileResponse(_CONSOLE, media_type="text/html")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness plus a cheap dependency probe.

    Deliberately does NOT call Bedrock: a health check that invokes a model bills on every
    ALB probe, which at a 30-second interval is 86,400 model calls a month for no
    information a config check does not already give.
    """
    config = get_config()
    checks = {
        "athena_workgroup": "configured" if config.athena_workgroup else "missing",
        "knowledge_base": "configured" if config.knowledge_base_id else "not configured",
        "guardrail": "enabled" if config.guardrail_id else "disabled",
        "state_machine": "configured" if config.state_machine_arn else "missing",
    }
    degraded = checks["athena_workgroup"] == "missing"

    return HealthResponse(
        status="degraded" if degraded else "ok",
        version=VERSION,
        region=config.region,
        checks=checks,
    )


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    return {"counters": dict(_counters), "version": VERSION}


@app.post("/ask", response_model=AskResponse)
def ask_endpoint(request: AskRequest) -> AskResponse:
    started = time.perf_counter()
    _counters["requests"] += 1

    try:
        result = ask(request.question)
    except Exception as exc:  # noqa: BLE001
        _counters["errors"] += 1
        logger.exception("graph invocation failed")
        raise HTTPException(status_code=500, detail=f"agent failed: {exc}") from exc

    elapsed_ms = (time.perf_counter() - started) * 1000
    _publish_metrics(result, elapsed_ms)

    for tool in result.get("tools_used", []):
        _counters[f"tool.{tool}"] += 1
    if result.get("stopped_reason"):
        _counters["max_iterations_hit"] += 1

    return AskResponse(**result)


@app.post("/ask/stream")
def ask_stream(request: AskRequest) -> StreamingResponse:
    """Stream graph progress as server-sent events.

    The graph takes seconds — schema introspection, SQL generation, an Athena round trip,
    then synthesis. Streaming node transitions turns that into visible progress instead of
    a spinner, and it is the same `stream_mode="updates"` the LangGraph runtime already
    provides, so no separate code path is maintained.
    """

    def event_stream():
        client = BedrockClient()
        graph = build_supervisor(client=client)
        started = time.perf_counter()
        final: dict[str, Any] = {}

        try:
            for update in graph.stream(
                {"question": request.question, "iterations": 0}, stream_mode="updates"
            ):
                for node, payload in update.items():
                    final.update(payload)
                    yield _sse("progress", {"node": node, "detail": _describe(node, payload)})

            elapsed_ms = (time.perf_counter() - started) * 1000
            answer = {
                "answer": final.get("answer", ""),
                "iterations": final.get("iterations", 0),
                "stopped_reason": final.get("stopped_reason"),
                "latency_ms": round(elapsed_ms, 1),
                "token_usage": client.usage.as_dict(),
            }
            _publish_metrics({**answer, "tools_used": []}, elapsed_ms)
            yield _sse("answer", answer)

        except Exception as exc:  # noqa: BLE001
            logger.exception("streaming graph failed")
            yield _sse("error", {"error": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/sql/check", response_model=SqlCheckResponse)
def sql_check(request: SqlCheckRequest) -> SqlCheckResponse:
    """Validate SQL without executing it.

    Exposed as a first-class endpoint because the validator is the security control worth
    demonstrating: a reviewer can point it at `DROP TABLE` or a silver-layer UNION and see
    the refusal, with no query cost.
    """
    try:
        validated = validate_sql(
            request.sql,
            allowed_tables=set(get_config().gold_tables),
            max_limit=get_config().max_rows_returned,
        )
    except SqlValidationError as exc:
        _counters["sql_rejected"] += 1
        return SqlCheckResponse(status="rejected", reason=str(exc))

    _counters["sql_accepted"] += 1
    return SqlCheckResponse(
        status="ok",
        sql=validated.sql,
        tables=validated.tables,
        limit_applied=validated.limit_applied,
        limit_was_injected=validated.limit_was_injected,
    )


# ------------------------------------------------------------------------ helpers


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _describe(node: str, payload: dict[str, Any]) -> str:
    if node == "route_query":
        tool = payload.get("next_tool", "none")
        return "gathering the answer" if tool == "none" else f"calling {tool}"
    if node == "call_tool":
        return f"ran tool (iteration {payload.get('iterations')})"
    return "composing the answer"


def _publish_metrics(result: dict[str, Any], elapsed_ms: float) -> None:
    """Per-invocation token usage, latency, and tool-call counts to CloudWatch."""
    config = get_config()
    if not config.emit_metrics:
        return

    usage = result.get("token_usage", {}) or {}
    data = [
        {"MetricName": "AgentLatencyMs", "Value": round(elapsed_ms, 1), "Unit": "Milliseconds"},
        {"MetricName": "AgentInputTokens", "Value": float(usage.get("input_tokens", 0)), "Unit": "Count"},
        {"MetricName": "AgentOutputTokens", "Value": float(usage.get("output_tokens", 0)), "Unit": "Count"},
        {"MetricName": "AgentModelCalls", "Value": float(usage.get("model_calls", 0)), "Unit": "Count"},
        {"MetricName": "AgentIterations", "Value": float(result.get("iterations", 0)), "Unit": "Count"},
    ]

    for tool in result.get("tools_used", []):
        data.append(
            {
                "MetricName": "ToolCalls",
                "Dimensions": [{"Name": "Tool", "Value": tool}],
                "Value": 1.0,
                "Unit": "Count",
            }
        )

    try:
        boto3.client("cloudwatch", region_name=config.region).put_metric_data(
            Namespace=config.metrics_namespace, MetricData=data
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a user request
        logger.warning("could not publish metrics: %s", exc)
