# Failure scenarios — runbook

Three exercises you run deliberately, screenshot, and write up. They are the difference
between "I built a pipeline" and "I know how my pipeline fails."

Each one has: **what to run**, **what to expect**, **what to capture**.

Before starting: `make stream-up`, and confirm one clean pipeline run is green.

---

## A. Schema evolution — the producer adds a field

**The claim being demonstrated:** an Iceberg table absorbs a new column without a rewrite
or a backfill, and queries against older snapshots still work.

### What to run

```bash
python -m ingestion.producer --stream $(terraform -chdir=terraform/envs/dev output -raw kinesis_stream_name) \
  --duration 120 --rate 50 --schema-version 2
```

Wait for the Firehose buffer (up to 5 minutes), then start the pipeline — or let the S3
trigger start it — and wait for it to go green.

### What to expect

The bronze job does **not** fail and does **not** need a code change. `_ensure_raw_columns`
adds `auth_response_code` as a typed NULL for older records, and Iceberg performs an
additive schema evolution on the table.

Confirm the column arrived:

```sql
DESCRIBE fraud_bronze.transactions;
```

Look at the snapshot history — every write is a snapshot, and the schema change is one of
them:

```sql
SELECT committed_at, snapshot_id, operation, summary['total-records'] AS total_records
FROM fraud_bronze."transactions$snapshots"
ORDER BY committed_at DESC;
```

Now the payoff — query the table **as it was before** the new column existed:

```sql
-- Replace with a snapshot id from before the v2 run.
SELECT count(*) AS rows_at_snapshot
FROM fraud_bronze.transactions FOR VERSION AS OF <old_snapshot_id>;
```

That query runs successfully against a schema that had no `auth_response_code`, which is
the whole point: the evolution is metadata, not a rewrite of history.

Then check the mix, which is only possible once both versions coexist:

```sql
SELECT schema_version,
       count(*) AS records,
       count(auth_response_code) AS with_auth_code
FROM fraud_bronze.transactions
GROUP BY schema_version;
```

You should see `schema_version = 1` rows with `with_auth_code = 0`, and
`schema_version = 2` rows with it populated.

### What to capture
1. `DESCRIBE` output showing the new column.
2. The `$snapshots` table with several snapshots and their `total-records`.
3. The time-travel query returning rows from a pre-evolution snapshot.
4. The `schema_version` breakdown showing old and new records in the same table.

### The interview answer
Iceberg tracks columns by ID, not by position, so adding a column is a metadata commit —
no data files are rewritten. That is also why *only additive* changes are safe here: a
rename or a type narrowing is a different, much more expensive conversation, and the
`test_v1_records_survive_the_v2_schema` test is what pins the additive property.

---

## B. Corrupted batch — quarantine, gate failure, skipped downstream

**The claim being demonstrated:** bad data is isolated with a reason attached, the gate
stops the pipeline, downstream layers keep their last good data, and the alarm fires.

### What to run

```bash
python -m ingestion.producer --stream $(terraform -chdir=terraform/envs/dev output -raw kinesis_stream_name) \
  --duration 60 --rate 50 --corrupt-rate 0.35
```

35% is deliberately far above anything realistic — the goal is an unmistakable gate
failure, not a subtle one.

### What to expect

1. The **bronze job succeeds.** This surprises people, and it is correct: isolating bad
   records is the job doing its work, not failing.
2. Roughly a third of records land in `quarantine/transactions/` with a
   `rejection_reason` naming every rule they broke.
3. The **bronze quality gate returns `passed: false`** — the `IsUnique`,
   `ReferentialIntegrity`, and `ColumnValues` rules trip.
4. `BronzeQualityChoice` takes the **Default** branch. Silver and gold never run.
5. A failure report lands in `failure-reports/dt=…/` and SNS emails you.
6. The `fraud-lake-quality-gate-failed-bronze` alarm goes into ALARM.

Inspect the damage:

```sql
SELECT rejection_reason, count(*) AS records
FROM fraud_quarantine.transactions          -- or read the prefix directly
GROUP BY rejection_reason
ORDER BY records DESC;
```

Confirm downstream is genuinely untouched:

```sql
SELECT max(dt) AS latest_gold_partition FROM fraud_gold.fraud_metrics_daily;
```

It should still show the **previous** run's date. That is the entire value of the gate:
gold is stale rather than wrong. Stale data with a fired alarm is recoverable; silently
wrong data in the table an agent answers questions from is not.

### Then reprocess

```bash
# Clean data for the same window
python -m ingestion.producer --stream <name> --duration 60 --rate 50
```

Re-run the state machine. The gate passes, silver and gold run, and gold's `max(dt)`
catches up. The quarantined records stay where they are — quarantine is an audit trail,
not a queue.

### What to capture
1. The Step Functions graph with the **Choice fork visibly taken to the failure branch**
   and silver/gold greyed out as never-entered. This is the single best screenshot in the
   project.
2. The quarantine breakdown by `rejection_reason`.
3. The SNS email.
4. The CloudWatch alarm in ALARM.
5. The same graph green after reprocessing.

---

## C. Duplicate replay — dedupe holds

**The claim being demonstrated:** at-least-once delivery is handled by design, so
re-delivering a batch does not change row counts.

### What to run

Capture a batch, then send it twice:

```bash
python -m ingestion.producer --out /tmp/batch.jsonl --count 5000 --rate 0 --seed 99
python -m ingestion.producer --replay /tmp/batch.jsonl --stream <name> --rate 200
```

Run the pipeline. Record the bronze count:

```sql
SELECT count(*) AS rows, count(DISTINCT transaction_id) AS unique_ids
FROM fraud_bronze.transactions;
```

Now replay the identical file:

```bash
python -m ingestion.producer --replay /tmp/batch.jsonl --stream <name> --rate 200
```

Run the pipeline again, and re-run the count.

### What to expect

`rows` and `unique_ids` are **unchanged and equal to each other**. Two independent
mechanisms enforce that, and it is worth being able to name both:

1. **In-batch**: `dedupe_transactions` keeps one row per `transaction_id`, the latest by
   `ingest_timestamp`.
2. **Across batches**: the bronze write is a `MERGE INTO … ON t.transaction_id =
   s.transaction_id`, not an append. A record already in the table is updated in place.

The append version of this pipeline would show 10,000 rows and 5,000 distinct ids — and
would look fine in every dashboard that counts rows.

The snapshot history shows the second run committed a snapshot with the same
`total-records`:

```sql
SELECT committed_at, operation, summary['total-records'] AS total_records
FROM fraud_bronze."transactions$snapshots"
ORDER BY committed_at DESC LIMIT 5;
```

`IsUnique "transaction_id"` in the bronze ruleset is the automated version of this check —
it runs on every execution, so a regression in the merge key fails the gate rather than
waiting for someone to notice.

### What to capture
1. Row count and distinct count before and after the replay — identical.
2. Two snapshots, same `total-records`.
3. The green pipeline run for the replay, showing the gate passing.

---

## Cleanup after the exercises

```bash
make stream-down
```

And if the schedule was armed for the demo, disarm it:

```bash
terraform -chdir=terraform/envs/dev apply -var enable_schedule=false
```

An hourly schedule left on runs Glue every hour over zero new rows — small per run, and
the most likely source of a surprising bill in this project after the Kinesis stream.
