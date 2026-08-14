"""Simulate delayed ground truth arriving — the feedback half of the learning loop.

In production these records would come from chargeback outcomes and analyst reviews,
days after scoring. Here we sample recent scored transactions and "confirm" their true
labels (with a small disagreement rate simulating analyst overrides), then land the
confirmations under feedback/ where the next training run folds them in.

    python scripts/simulate_feedback.py --sample 800 --override-rate 0.05
"""

from __future__ import annotations

import argparse
import json
import random
import time
from datetime import UTC, datetime

import boto3

BUCKET = "fraud-lake-434661699277"
WORKGROUP = "fraud-lake"


def athena_rows(client, sql: str) -> list[dict]:
    qid = client.start_query_execution(
        QueryString=sql,
        WorkGroup=WORKGROUP,
        QueryExecutionContext={"Database": "fraud_gold"},
    )["QueryExecutionId"]
    while True:
        state = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if state != "SUCCEEDED":
        raise RuntimeError(f"query {state}")
    result = client.get_query_results(QueryExecutionId=qid, MaxResults=1000)["ResultSet"]
    header = [c["VarCharValue"] for c in result["Rows"][0]["Data"]]
    return [
        dict(zip(header, [f.get("VarCharValue") for f in r["Data"]], strict=False))
        for r in result["Rows"][1:]
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=800, help="transactions to confirm")
    parser.add_argument(
        "--override-rate",
        type=float,
        default=0.05,
        help="fraction where the analyst disagrees with the pipeline label",
    )
    parser.add_argument("--profile", default="fraud-lake")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    rows = athena_rows(
        session.client("athena"),
        f"SELECT transaction_id, actual_is_fraud FROM fraud_gold.transaction_risk_scores "
        f"ORDER BY scored_at DESC LIMIT {args.sample}",
    )

    rng = random.Random()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines = []
    overrides = 0
    for row in rows:
        truth = row["actual_is_fraud"] == "true"
        if rng.random() < args.override_rate:
            truth = not truth
            overrides += 1
        lines.append(
            json.dumps(
                {
                    "transaction_id": row["transaction_id"],
                    "confirmed_is_fraud": truth,
                    "confirmed_at": now,
                    "source": "simulated_chargeback_outcome",
                }
            )
        )

    key = f"feedback/dt={now[:10]}/confirmations-{now.replace(':', '')}.jsonl"
    session.client("s3").put_object(
        Bucket=BUCKET, Key=key, Body="\n".join(lines).encode(), ContentType="application/json"
    )
    print(f"wrote {len(lines)} confirmations ({overrides} overrides) to s3://{BUCKET}/{key}")
    print("next training run (make train) will fold these into the labels")


if __name__ == "__main__":
    main()
