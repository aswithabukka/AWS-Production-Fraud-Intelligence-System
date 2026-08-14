"""Pydantic request/response models for the agent API.

Typed at the boundary so a malformed request is a 422 from the framework rather than a
KeyError three layers into the graph.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        # An upper bound is a cost control as much as a validation rule: the question is
        # prepended to every routing turn, so a 50 KB "question" is billed repeatedly.
        max_length=2_000,
        description="Natural-language question about fraud data, policy, or pipeline health.",
        examples=["Compare fraud rate by MCC for the last 30 days versus the prior 30."],
    )
    stream: bool = Field(default=False, description="Stream progress events as they happen.")
    conversation_id: str | None = Field(
        default=None,
        max_length=64,
        description="Set to any stable id to enable multi-turn memory — follow-ups see prior tool results.",
    )


class Citation(BaseModel):
    document: str
    source: str
    score: float
    text: str


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    model_latency_ms: float = 0.0
    by_model: dict[str, int] = Field(default_factory=dict)


class AskResponse(BaseModel):
    question: str
    answer: str
    tools_used: list[str] = Field(default_factory=list)
    sql_executed: list[str] = Field(
        default_factory=list,
        description="The SQL that actually ran, post-validation. Returned so the answer is checkable.",
    )
    citations: list[Citation] = Field(default_factory=list)
    iterations: int = 0
    stopped_reason: str | None = Field(
        default=None,
        description="Set when the loop hit MAX_ITERATIONS instead of finishing naturally.",
    )
    latency_ms: float = 0.0
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    trace: list[dict[str, Any]] = Field(default_factory=list)


class SqlCheckRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=20_000)


class SqlCheckResponse(BaseModel):
    status: Literal["ok", "rejected"]
    sql: str | None = None
    tables: list[str] = Field(default_factory=list)
    limit_applied: int | None = None
    limit_was_injected: bool = False
    reason: str | None = None


class ScoreRequest(BaseModel):
    """A single event to score in real time.

    Only `amount` is required. History-dependent features (velocity, z-score, geo)
    come from the online feature store when a customer_id is given, from the caller
    when provided explicitly, and default to absent-with-indicator otherwise.
    """

    transaction_id: str | None = None
    customer_id: str | None = None
    amount: float = Field(..., gt=0)
    channel: str | None = None
    merchant_risk_score: float | None = None
    geo_distance_from_prior_km: float | None = None
    implied_speed_kmh: float | None = None
    device_change_flag: bool | None = None
    txn_count_1h: int | None = None
    txn_count_24h: int | None = None
    amount_zscore_30d: float | None = None


class ScoreResponse(BaseModel):
    ensemble_fraud_score: float
    predicted_is_fraud: bool
    decision_threshold: float
    model_scores: dict[str, float]
    top_risk_factors: list[str]
    features_from_store: list[str]
    model_run_id: str
    latency_ms: float


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    region: str
    checks: dict[str, str] = Field(default_factory=dict)
