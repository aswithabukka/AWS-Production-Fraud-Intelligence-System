"""Bedrock Converse API wrapper with guardrails and per-invocation telemetry.

One place where every model call happens, so token accounting, guardrail application, and
retry behaviour cannot be forgotten at an individual call site.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config

from agents.config import AgentConfig, get_config

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    latency_ms_total: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, model_id: str, usage: dict[str, int], latency_ms: float) -> None:
        self.input_tokens += usage.get("inputTokens", 0)
        self.output_tokens += usage.get("outputTokens", 0)
        self.calls += 1
        self.latency_ms_total += latency_ms
        self.by_model[model_id] = self.by_model.get(model_id, 0) + usage.get("totalTokens", 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "model_calls": self.calls,
            "model_latency_ms": round(self.latency_ms_total, 1),
            "by_model": self.by_model,
        }


@dataclass
class ModelResponse:
    text: str
    stop_reason: str
    usage: dict[str, int]
    latency_ms: float
    guardrail_intervened: bool = False


class BedrockClient:
    """Thin, explicit wrapper over `bedrock-runtime.converse`.

    On-demand invocation only. Provisioned throughput is an hourly commitment and is
    never used in this project.
    """

    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or get_config()
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=self.config.region,
            config=Config(
                # Bedrock throttles on demand spikes; adaptive retries back off rather
                # than hammering, which is the difference between a slow answer and a
                # failed one.
                retries={"max_attempts": 3, "mode": "adaptive"},
                read_timeout=120,
                connect_timeout=10,
            ),
        )
        self.usage = TokenUsage()

    def converse(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        model_id: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        tools: list[dict[str, Any]] | None = None,
        apply_guardrail: bool = True,
    ) -> ModelResponse:
        model = model_id or self.config.routing_model_id
        request: dict[str, Any] = {
            "modelId": model,
            "messages": messages,
            "inferenceConfig": {
                # Always capped. An uncapped max_tokens turns one runaway generation into
                # a real line item.
                "maxTokens": max_tokens or self.config.routing_max_tokens,
                # Deterministic by default: SQL generation and routing are not creative
                # tasks, and a non-zero temperature makes failures unreproducible.
                "temperature": temperature,
            },
        }

        if system:
            request["system"] = [{"text": system}]

        if tools:
            request["toolConfig"] = {"tools": tools}

        if apply_guardrail and self.config.guardrail_id:
            request["guardrailConfig"] = {
                "guardrailIdentifier": self.config.guardrail_id,
                "guardrailVersion": self.config.guardrail_version,
            }

        started = time.perf_counter()
        response = self._client.converse(**request)
        latency_ms = (time.perf_counter() - started) * 1000

        usage = response.get("usage", {})
        self.usage.add(model, usage, latency_ms)

        stop_reason = response.get("stopReason", "end_turn")
        text = _extract_text(response)

        if stop_reason == "guardrail_intervened":
            logger.warning("guardrail intervened on a %s call", model)

        return ModelResponse(
            text=text,
            stop_reason=stop_reason,
            usage=usage,
            latency_ms=latency_ms,
            guardrail_intervened=stop_reason == "guardrail_intervened",
        )


def _extract_text(response: dict[str, Any]) -> str:
    content = response.get("output", {}).get("message", {}).get("content", [])
    return "".join(block.get("text", "") for block in content).strip()


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"text": text}]}
