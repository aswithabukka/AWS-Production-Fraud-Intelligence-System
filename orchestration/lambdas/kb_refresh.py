"""Kicks off a Bedrock Knowledge Base ingestion so retrieval reflects the current corpus.

Deliberately non-fatal. The state machine catches every error from this state and still
routes to success, because stale retrieval is a degraded answer while a failed pipeline
over good data is a false alarm that trains you to ignore alerts.

Also tolerates the Knowledge Base not existing yet: slice 1c is built and demonstrated
before slice 2 creates it, and the pipeline must run green in between.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.exceptions import ClientError


def handler(_event: dict[str, Any] | None = None, _context: Any = None) -> dict[str, Any]:
    knowledge_base_id = os.environ.get("KNOWLEDGE_BASE_ID", "")
    data_source_id = os.environ.get("DATA_SOURCE_ID", "")

    if not knowledge_base_id or not data_source_id:
        return {
            "status": "skipped",
            "detail": "no knowledge base configured yet (created in slice 2)",
        }

    client = boto3.client("bedrock-agent")
    try:
        response = client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            description="fraud-lake pipeline refresh",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code == "ConflictException":
            # An ingestion job is already running. That is the desired end state anyway.
            return {"status": "already_running", "detail": "an ingestion job is in progress"}
        return {"status": "error", "detail": f"{code}: {exc}"}

    job = response["ingestionJob"]
    return {"status": "started", "detail": job["ingestionJobId"]}
