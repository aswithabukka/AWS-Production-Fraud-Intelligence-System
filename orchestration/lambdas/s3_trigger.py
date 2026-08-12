"""Starts the pipeline when Firehose delivers a new object to the raw zone.

The event-driven path, running alongside the hourly schedule. Two guards matter here:

1. **Prefix filtering.** The lake bucket receives writes from Glue, Athena, and Spark
   event logs as well as Firehose. Without a prefix check the pipeline would trigger on
   its own output — an infinite loop that bills real money.

2. **Execution-name idempotency.** Firehose delivers many objects per buffer window and
   each one raises an event. Naming the execution after the partition means concurrent
   triggers for the same dt collapse into one run: Step Functions rejects a duplicate
   name with ExecutionAlreadyExists, which this function swallows.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote_plus

import boto3

sfn = boto3.client("stepfunctions")

WATCHED_PREFIX = "raw/transactions/"
PARTITION_PATTERN = re.compile(r"dt=(\d{4}-\d{2}-\d{2})")

# Coarse enough that a 5-minute Firehose buffer window collapses into one execution,
# fine enough that a genuinely new batch an hour later gets its own run.
BUCKET_MINUTES = 15


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    state_machine_arn = os.environ["STATE_MACHINE_ARN"]
    started, skipped = [], []

    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])

        if not key.startswith(WATCHED_PREFIX):
            skipped.append({"key": key, "reason": "outside the raw transactions prefix"})
            continue

        match = PARTITION_PATTERN.search(key)
        partition = match.group(1) if match else datetime.now(UTC).strftime("%Y-%m-%d")

        name = _execution_name(partition)
        try:
            response = sfn.start_execution(
                stateMachineArn=state_machine_arn,
                name=name,
                input=_input_payload(partition, key),
            )
            started.append(response["executionArn"])
        except sfn.exceptions.ExecutionAlreadyExists:
            # Expected and healthy: another object in the same buffer window won the race.
            skipped.append({"key": key, "reason": f"execution {name} already running"})

    return {"started": started, "skipped": skipped}


def _execution_name(partition: str) -> str:
    now = datetime.now(UTC)
    bucket = (now.hour * 60 + now.minute) // BUCKET_MINUTES
    return f"s3-{partition}-{now:%Y%m%d}-{bucket:03d}"


def _input_payload(partition: str, key: str) -> str:
    import json

    return json.dumps(
        {
            "trigger": "s3",
            "process_date": partition,
            "source_key": key,
            "requested_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
