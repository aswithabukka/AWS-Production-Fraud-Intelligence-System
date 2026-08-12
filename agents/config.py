"""Runtime configuration for the agent layer.

Everything is environment-driven with safe defaults so the same code runs locally, in
ECS, and in Lambda without a branch. No account IDs, ARNs, or secrets are hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class AgentConfig:
    region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))

    # ------------------------------------------------------------------ model choice
    #
    # Two tiers, deliberately. Routing and SQL generation are structured, low-creativity
    # tasks where a fast model is both cheaper and lower latency; only the final synthesis
    # — turning rows and citations into prose a human reads — justifies the larger model.
    # Using one large model everywhere is the single easiest way to make a Bedrock bill
    # grow without making answers better.
    routing_model_id: str = field(
        default_factory=lambda: _env("ROUTING_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    )
    sql_model_id: str = field(
        default_factory=lambda: _env("SQL_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    )
    synthesis_model_id: str = field(
        default_factory=lambda: _env("SYNTHESIS_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    )

    # ---------------------------------------------------------------- token budgets
    #
    # Every call is capped. An unbounded max_tokens turns one confused loop into a
    # genuinely expensive afternoon.
    routing_max_tokens: int = field(default_factory=lambda: _env_int("ROUTING_MAX_TOKENS", 512))
    sql_max_tokens: int = field(default_factory=lambda: _env_int("SQL_MAX_TOKENS", 1_024))
    synthesis_max_tokens: int = field(default_factory=lambda: _env_int("SYNTHESIS_MAX_TOKENS", 2_048))

    # The hard stop on the LangGraph loop, enforced inside the routing logic rather than
    # relying on a graph edge to terminate. A model that keeps deciding it needs one more
    # tool call is the failure mode that bills.
    max_iterations: int = field(default_factory=lambda: _env_int("MAX_ITERATIONS", 5))

    # ------------------------------------------------------------------- data access
    athena_workgroup: str = field(default_factory=lambda: _env("ATHENA_WORKGROUP", "fraud-lake"))
    athena_database: str = field(default_factory=lambda: _env("ATHENA_DATABASE", "fraud_gold"))
    athena_output_location: str = field(default_factory=lambda: _env("ATHENA_OUTPUT_LOCATION"))
    athena_timeout_seconds: int = field(default_factory=lambda: _env_int("ATHENA_TIMEOUT_SECONDS", 60))

    gold_tables: tuple[str, ...] = (
        "fraud_gold.fraud_metrics_daily",
        "fraud_gold.merchant_risk",
    )
    max_rows_returned: int = field(default_factory=lambda: _env_int("MAX_ROWS_RETURNED", 200))

    # ---------------------------------------------------------------------- retrieval
    knowledge_base_id: str = field(default_factory=lambda: _env("KNOWLEDGE_BASE_ID"))
    retrieval_results: int = field(default_factory=lambda: _env_int("RETRIEVAL_RESULTS", 5))

    # --------------------------------------------------------------------- guardrails
    guardrail_id: str = field(default_factory=lambda: _env("GUARDRAIL_ID"))
    guardrail_version: str = field(default_factory=lambda: _env("GUARDRAIL_VERSION", "DRAFT"))

    # ------------------------------------------------------------------ observability
    state_machine_arn: str = field(default_factory=lambda: _env("STATE_MACHINE_ARN"))
    glue_job_prefix: str = field(default_factory=lambda: _env("GLUE_JOB_PREFIX", "fraud-lake"))
    metrics_namespace: str = field(default_factory=lambda: _env("METRICS_NAMESPACE", "fraud-lake/agent"))
    emit_metrics: bool = field(default_factory=lambda: _env("EMIT_METRICS", "true").lower() == "true")

    # Schema introspection is a Glue API call per turn unless cached. The catalog changes
    # when a Glue job runs, not when a user asks a question, so a long TTL is correct.
    schema_cache_ttl_seconds: int = field(default_factory=lambda: _env_int("SCHEMA_CACHE_TTL", 3_600))


@lru_cache(maxsize=1)
def get_config() -> AgentConfig:
    return AgentConfig()
