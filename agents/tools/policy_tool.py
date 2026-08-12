"""search_policies — retrieval over the fraud-policy corpus, with citations.

Bedrock Knowledge Base backed by **S3 Vectors**. The vector store choice is the single
most important cost decision in the project: OpenSearch Serverless Classic has a ~2 OCU
minimum billed while completely idle, and deleting the Knowledge Base does not delete the
collection it created — it keeps billing from a console you are no longer looking at.
S3 Vectors is per-request: storage plus a per-query fee, no idle compute.

Citations are non-negotiable. A policy answer without a source is an assertion, and the
whole reason to ground on a corpus is so the reader can check it.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3

from agents.config import get_config

logger = logging.getLogger(__name__)

SEARCH_POLICIES_SPEC = {
    "toolSpec": {
        "name": "search_policies",
        "description": (
            "Search the fraud-policy document corpus — chargeback rules, dispute windows, "
            "liability shift, merchant onboarding requirements, transaction monitoring "
            "thresholds. Use for questions about what the policy says or requires, as "
            "opposed to what the data shows. Returns passages with source citations."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for in the policy corpus.",
                    }
                },
                "required": ["query"],
            }
        },
    }
}


def search_policies(query: str, max_results: int | None = None) -> dict[str, Any]:
    config = get_config()

    if not config.knowledge_base_id:
        return {
            "status": "unavailable",
            "error": "no Bedrock Knowledge Base is configured (KNOWLEDGE_BASE_ID is unset)",
            "results": [],
        }

    client = boto3.client("bedrock-agent-runtime", region_name=config.region)

    try:
        response = client.retrieve(
            knowledgeBaseId=config.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": max_results or config.retrieval_results,
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 - a retrieval failure degrades, never 500s
        logger.warning("knowledge base retrieval failed: %s", exc)
        return {"status": "error", "error": str(exc), "results": []}

    results = []
    for item in response.get("retrievalResults", []):
        location = item.get("location", {})
        source = (
            location.get("s3Location", {}).get("uri")
            or location.get("webLocation", {}).get("url")
            or "unknown"
        )
        results.append(
            {
                "text": item.get("content", {}).get("text", ""),
                "source": source,
                # The relevance score travels with the passage so the synthesis step can
                # hedge on weak matches instead of stating a thin retrieval as fact.
                "score": round(item.get("score", 0.0), 4),
                "document": source.rsplit("/", 1)[-1],
            }
        )

    return {
        "status": "ok",
        "query": query,
        "result_count": len(results),
        "results": results,
    }


def format_citations(results: list[dict[str, Any]]) -> str:
    """Render passages for the synthesis prompt with explicit citation markers.

    Numbered markers rather than raw URIs: the model reproduces `[1]` reliably and
    mangles long S3 URIs, and the mapping back to sources is done here in code where it
    cannot be hallucinated.
    """
    if not results:
        return "(no policy passages retrieved)"

    blocks = []
    for index, item in enumerate(results, start=1):
        blocks.append(f"[{index}] source: {item['document']} (relevance {item['score']})\n{item['text']}")
    return "\n\n".join(blocks)
