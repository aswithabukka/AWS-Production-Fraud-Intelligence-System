"""Synthetic transaction producer.

Two sinks:

    --dry-run / --out FILE   stdout or a local JSONL file. No AWS calls, no cost.
    --stream NAME            Kinesis Data Stream, batched via put_records.

Usage:

    python -m ingestion.producer --dry-run --duration 5 --rate 10
    python -m ingestion.producer --stream fraud-lake-transactions --duration 60 --rate 50
    python -m ingestion.producer --stream ... --schema-version 2      # schema-evolution demo
    python -m ingestion.producer --stream ... --corrupt-rate 0.15     # quarantine demo
    python -m ingestion.producer --replay events.jsonl --stream ...   # duplicate-replay demo

The three demo flags exist because slice 1c's failure scenarios need to be reproducible on
demand, not waited for.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from ingestion.generator import GeneratorConfig, TransactionGenerator

# Kinesis put_records hard limits: 500 records or 5 MB per call.
MAX_RECORDS_PER_BATCH = 500
MAX_BATCH_BYTES = 4 * 1024 * 1024  # 4 MB, leaving headroom under the 5 MB ceiling

_shutdown = False


def _handle_sigint(signum, frame) -> None:  # pragma: no cover - signal path
    global _shutdown
    _shutdown = True
    print("\nshutting down, flushing final batch...", file=sys.stderr)


def corrupt(event: dict, rng: random.Random) -> dict:
    """Damage an event in one of the ways the bronze job must survive.

    Each mode maps to a specific bronze-layer rejection reason, so the quarantine table
    can be checked for the exact distribution that was injected.
    """
    modes = (
        "null_transaction_id",
        "missing_customer_id",
        "non_numeric_amount",
        "negative_amount",
        "bad_timestamp",
        "unknown_merchant",
        "out_of_range_geo",
    )
    broken = dict(event)
    mode = rng.choice(modes)
    if mode == "null_transaction_id":
        broken["transaction_id"] = None
    elif mode == "missing_customer_id":
        broken.pop("customer_id", None)
    elif mode == "non_numeric_amount":
        broken["amount"] = "N/A"
    elif mode == "negative_amount":
        broken["amount"] = -abs(broken.get("amount", 10.0))
    elif mode == "bad_timestamp":
        broken["timestamp"] = "not-a-timestamp"
    elif mode == "unknown_merchant":
        broken["merchant_id"] = "MER999999"
    elif mode == "out_of_range_geo":
        broken["lat"] = 181.5
        broken["lon"] = -420.0
    broken["_corruption_mode"] = mode  # only present in synthetic data; bronze ignores it
    return broken


class KinesisSink:
    """Batched Kinesis writer with retry on partial failures.

    put_records can partially fail — some records in the batch succeed and others come
    back with an error code. Silently ignoring that is the most common bug in producer
    code, so failed records are collected and retried with backoff.
    """

    def __init__(self, stream_name: str, region: str, profile: str | None = None) -> None:
        import boto3  # imported lazily so --dry-run needs no boto3 credentials

        session = boto3.Session(profile_name=profile, region_name=region)
        self.client = session.client("kinesis")
        self.stream_name = stream_name
        self.sent = 0
        self.failed = 0
        self._buffer: list[dict[str, Any]] = []
        self._buffer_bytes = 0

    def write(self, event: dict) -> None:
        blob = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        if self._buffer and (
            len(self._buffer) >= MAX_RECORDS_PER_BATCH or self._buffer_bytes + len(blob) > MAX_BATCH_BYTES
        ):
            self.flush()
        # Partition by customer so one customer's events land on one shard in order —
        # which is what makes the per-customer velocity features meaningful downstream.
        self._buffer.append({"Data": blob, "PartitionKey": str(event.get("customer_id", "unknown"))})
        self._buffer_bytes += len(blob)

    def flush(self) -> None:
        if not self._buffer:
            return
        records = self._buffer
        self._buffer, self._buffer_bytes = [], 0

        for attempt in range(5):
            response = self.client.put_records(StreamName=self.stream_name, Records=records)
            failed_count = response.get("FailedRecordCount", 0)
            if not failed_count:
                self.sent += len(records)
                return
            # Retry only the records that actually failed. strict=True: Kinesis returns
            # one result per record in order, and a length mismatch would silently
            # misalign errors with records.
            retry = [
                rec for rec, res in zip(records, response["Records"], strict=True) if res.get("ErrorCode")
            ]
            self.sent += len(records) - len(retry)
            records = retry
            time.sleep(0.2 * (2**attempt))

        self.failed += len(records)
        print(f"WARN: {len(records)} records failed after retries", file=sys.stderr)

    def close(self) -> None:
        self.flush()


class FileSink:
    """Writes JSONL to a file or stdout. No AWS, no cost."""

    def __init__(self, path: str | None) -> None:
        # Held open for the sink's lifetime and closed in close() — a context manager
        # would have to wrap the whole producer loop instead.
        self.handle = sys.stdout if path in (None, "-") else open(path, "w", encoding="utf-8")  # noqa: SIM115
        self._owns_handle = self.handle is not sys.stdout
        self.sent = 0
        self.failed = 0

    def write(self, event: dict) -> None:
        self.handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        self.sent += 1

    def flush(self) -> None:
        self.handle.flush()

    def close(self) -> None:
        self.flush()
        if self._owns_handle:
            self.handle.close()


def replay_events(path: str) -> Iterator[dict]:
    """Re-emit a previously captured JSONL file verbatim.

    Used for the duplicate-replay scenario: the same transaction_ids are delivered twice
    and bronze's dedupe must hold row counts flat.
    """
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingestion.producer",
        description="Generate synthetic card transactions into Kinesis, a file, or stdout.",
    )
    parser.add_argument("--rate", type=float, default=50.0, help="events per second (default: 50)")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds to run (default: 60)")
    parser.add_argument("--count", type=int, default=None, help="stop after N events (overrides --duration)")
    parser.add_argument(
        "--fraud-rate", type=float, default=0.015, help="fraction of fraudulent transactions (default: 0.015)"
    )
    parser.add_argument(
        "--schema-version",
        type=int,
        default=1,
        choices=(1, 2),
        help="2 emits the additional auth_response_code field (Iceberg schema-evolution demo)",
    )
    parser.add_argument(
        "--corrupt-rate",
        type=float,
        default=0.0,
        help="fraction of records deliberately malformed (quarantine demo)",
    )
    parser.add_argument("--customers", type=int, default=2000, help="size of the customer pool")
    parser.add_argument("--merchants", type=int, default=500, help="size of the merchant pool")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")

    sink = parser.add_argument_group("sink")
    sink.add_argument("--stream", help="Kinesis stream name (omit for a local sink)")
    sink.add_argument("--region", default="us-east-1")
    sink.add_argument("--profile", default=None, help="AWS profile (default: env / fraud-lake)")
    sink.add_argument("--out", help="write JSONL to this file instead of AWS")
    sink.add_argument("--dry-run", action="store_true", help="print JSONL to stdout, no AWS calls")
    sink.add_argument("--replay", help="re-emit a JSONL file verbatim (duplicate-replay demo)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    signal.signal(signal.SIGINT, _handle_sigint)

    if not args.dry_run and not args.out and not args.stream:
        print("error: pass --stream, --out, or --dry-run", file=sys.stderr)
        return 2

    if args.stream and not (args.dry_run or args.out):
        sink: Any = KinesisSink(args.stream, args.region, args.profile)
        target = f"kinesis://{args.stream}"
    else:
        sink = FileSink(args.out if args.out else None)
        target = args.out or "stdout"

    rng = random.Random(args.seed)
    started = time.monotonic()
    stats = {"emitted": 0, "fraud": 0, "corrupted": 0}

    def event_source() -> Iterator[dict]:
        if args.replay:
            yield from replay_events(args.replay)
            return
        generator = TransactionGenerator(
            GeneratorConfig(
                fraud_rate=args.fraud_rate,
                n_customers=args.customers,
                n_merchants=args.merchants,
                schema_version=args.schema_version,
                seed=args.seed,
            )
        )
        while True:
            yield generator.next_event()

    interval = 1.0 / args.rate if args.rate > 0 else 0.0
    next_emit = time.monotonic()

    print(
        f"producer -> {target} | rate={args.rate}/s duration={args.duration}s "
        f"fraud_rate={args.fraud_rate} schema_version={args.schema_version}"
        + (f" corrupt_rate={args.corrupt_rate}" if args.corrupt_rate else ""),
        file=sys.stderr,
    )

    try:
        for event in event_source():
            if _shutdown:
                break
            if args.count is not None:
                if stats["emitted"] >= args.count:
                    break
            elif time.monotonic() - started >= args.duration:
                break

            if args.corrupt_rate and rng.random() < args.corrupt_rate:
                event = corrupt(event, rng)
                stats["corrupted"] += 1
            if event.get("is_fraud"):
                stats["fraud"] += 1

            sink.write(event)
            stats["emitted"] += 1

            if stats["emitted"] % 500 == 0:
                sink.flush()
                elapsed = time.monotonic() - started
                print(
                    f"  {stats['emitted']} events | {stats['emitted'] / max(elapsed, 1e-9):.1f}/s",
                    file=sys.stderr,
                )

            if interval:
                next_emit += interval
                sleep_for = next_emit - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    # Falling behind the requested rate — don't accumulate debt.
                    next_emit = time.monotonic()
    finally:
        sink.close()

    elapsed = time.monotonic() - started
    summary = {
        "target": target,
        "emitted": stats["emitted"],
        "fraud": stats["fraud"],
        "fraud_pct": round(100 * stats["fraud"] / max(stats["emitted"], 1), 3),
        "corrupted": stats["corrupted"],
        "delivered": getattr(sink, "sent", stats["emitted"]),
        "failed": getattr(sink, "failed", 0),
        "elapsed_s": round(elapsed, 2),
        "throughput_eps": round(stats["emitted"] / max(elapsed, 1e-9), 1),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    print(json.dumps(summary, indent=2), file=sys.stderr)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
