"""Reads a quality report from S3 and returns a verdict the state machine branches on.

Why a Lambda rather than branching on the Glue job's exit status: a failing quality gate
is the system *working*. If the gate failed the Glue task, the execution graph would show
a red error state, and the corrupted-batch demo would look like a broken pipeline instead
of a pipeline that correctly refused to promote bad data. A Choice state on
`$.quality.passed` renders as a visible fork, which is exactly the story worth screenshotting.

Trigger and light validation only — no heavy processing ever happens in this function.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import boto3

s3 = boto3.client("s3")

# A missing report means the quality job did not run to completion. Treating that as a
# pass would let an unvalidated batch through on an infrastructure failure — the exact
# situation the gate exists for. Fail closed.
FAIL_CLOSED = True


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    layer = event.get("layer", "bronze")
    report_prefix = event.get("report_path") or os.environ["REPORT_PREFIX"]

    uri = f"{report_prefix.rstrip('/')}/{layer}/report.json"
    parsed = urlparse(uri)

    try:
        body = s3.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"].read()
        report = json.loads(body)
    except s3.exceptions.NoSuchKey:
        return _missing(layer, uri, "quality report not found")
    except json.JSONDecodeError as exc:
        return _missing(layer, uri, f"quality report is not valid JSON: {exc}")

    return {
        "layer": layer,
        "passed": bool(report.get("passed", not FAIL_CLOSED)),
        "pass_rate_pct": report.get("pass_rate_pct", 0.0),
        "failed_count": report.get("failed_count", 0),
        "total_rules": report.get("total_rules", 0),
        # Truncated deliberately: the whole verdict travels through Step Functions state,
        # which has a 256 KB payload limit. The full report stays in S3.
        "failed_rules": report.get("failed_rules", [])[:10],
        "report_uri": uri,
        "evaluated_at": report.get("evaluated_at"),
    }


def _missing(layer: str, uri: str, reason: str) -> dict[str, Any]:
    return {
        "layer": layer,
        "passed": not FAIL_CLOSED,
        "pass_rate_pct": 0.0,
        "failed_count": -1,
        "total_rules": 0,
        "failed_rules": [{"rule": "report_available", "reason": reason}],
        "report_uri": uri,
        "evaluated_at": None,
    }
