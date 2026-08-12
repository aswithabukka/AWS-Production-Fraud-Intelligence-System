"""Writes a human-readable failure report and publishes it to SNS.

Invoked from the state machine's failure branch — either a quality gate that returned
`passed: false`, or a Catch block on a Glue task error. The report is what someone reads
at 3am, so it says what failed, why, what was quarantined, and what to do next.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import boto3

s3 = boto3.client("s3")
sns = boto3.client("sns")

REMEDIATION = {
    "quality_gate": (
        "Inspect the quarantine prefix for the rejection breakdown, fix the producer or the "
        "bronze validation rule that disagrees with it, then re-run the pipeline for the "
        "affected dt partition. Downstream layers were deliberately skipped, so silver and "
        "gold still hold the last known-good data."
    ),
    "glue_error": (
        "Open the Glue job run in the console and read the error from the continuous log "
        "group. The state machine already retried with backoff, so this is a persistent "
        "failure, not a transient one."
    ),
}


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    failure_type = event.get("failure_type", "quality_gate")
    execution_arn = event.get("execution_arn", "unknown")
    quality = event.get("quality", {})
    error = event.get("error", {})

    now = datetime.now(UTC)
    report = {
        "failure_type": failure_type,
        "detected_at": now.isoformat(timespec="seconds"),
        "execution_arn": execution_arn,
        "stage": event.get("stage", "unknown"),
        "quality_verdict": quality,
        "error": error,
        "remediation": REMEDIATION.get(failure_type, "Inspect the execution history."),
        "downstream_skipped": True,
    }

    key = f"failure-reports/dt={now:%Y-%m-%d}/{now:%H%M%S}-{failure_type}.json"
    bucket = urlparse(os.environ["LAKE_BUCKET_URI"]).netloc or os.environ["LAKE_BUCKET_URI"]

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(report, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )

    sns.publish(
        TopicArn=os.environ["SNS_TOPIC_ARN"],
        Subject=f"[fraud-lake] pipeline halted: {failure_type}"[:100],
        Message=_format_message(report, bucket, key),
    )

    return {"report_uri": f"s3://{bucket}/{key}", **report}


def _format_message(report: dict[str, Any], bucket: str, key: str) -> str:
    quality = report.get("quality_verdict") or {}
    failed_rules = quality.get("failed_rules") or []

    lines = [
        f"fraud-lake pipeline halted at stage: {report['stage']}",
        f"reason: {report['failure_type']}",
        f"detected: {report['detected_at']}",
        f"execution: {report['execution_arn']}",
        "",
    ]

    if failed_rules:
        lines.append(f"failed data-quality rules ({quality.get('failed_count')}):")
        lines += [f"  - {r.get('rule')}: {r.get('reason')}" for r in failed_rules]
        lines.append("")

    if report.get("error"):
        lines += [f"error: {report['error']}", ""]

    lines += [
        "Downstream layers were skipped — silver and gold still hold the last good data.",
        "",
        report["remediation"],
        "",
        f"full report: s3://{bucket}/{key}",
    ]
    return "\n".join(lines)
