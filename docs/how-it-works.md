# fraud-lake — How the System Works

A component-by-component explanation of the platform: what each AWS service does here,
why it was chosen, how data moves through it, and what dataset feeds it.

---

## 1. The system in one paragraph

Synthetic card transactions are streamed into **Kinesis**, landed in **S3** by
**Firehose**, and refined by three **Glue PySpark** jobs into **Apache Iceberg** tables
(bronze → silver → gold) registered in the **Glue Data Catalog** and queried by
**Athena**. **Step Functions** orchestrates the jobs with **Glue Data Quality** gates
that quarantine bad data and halt downstream processing. On top sits a **LangGraph**
multi-agent system on **Bedrock**: it turns natural-language questions into validated
SQL against the gold layer, retrieves fraud-policy passages from a **Bedrock Knowledge
Base** backed by **S3 Vectors**, and reads pipeline health from Step Functions history —
exposed through an **MCP server** and a **FastAPI** service, containerized to **ECS
Fargate** (with a same-day **EKS** demo), all provisioned by **Terraform** and deployed
by **GitHub Actions**.

## 2. The life of one transaction

The clearest way to understand the backend is to follow a single event through it.

```
 producer.py                        (your laptop / anywhere)
     │  JSON: {transaction_id, customer_id, amount, lat/lon, is_fraud, ...}
     ▼
 Kinesis Data Stream                partition key = customer_id
     │                              → one customer's events stay ordered
     ▼
 Kinesis Firehose                   buffers 5 MB / 300 s, GZIP
     │
     ▼
 s3://fraud-lake-<acct>/raw/transactions/dt=2026-08-12/    (JSON, immutable)
     │
     │   S3 event ──▶ Lambda ──▶ starts Step Functions   (or hourly schedule)
     ▼
 ┌─────────────────────────── Step Functions ────────────────────────────┐
 │  bronze job ─▶ quality gate ─▶ silver job ─▶ quality gate ─▶ gold job │
 │       │             │                                                 │
 │       ▼             ▼ on fail: report → SNS email → downstream SKIPPED│
 │  quarantine/   (gold stays stale, never wrong)                        │
 └───────────────────────────────────────────────────────────────────────┘
     ▼
 fraud_gold.fraud_metrics_daily / merchant_risk        (Iceberg, in Athena)
     ▼
 LangGraph agent  ◀── "Compare fraud rate by MCC, last 30 vs prior 30 days"
     │  route → query_lakehouse → SQL validator → Athena → synthesize
     ▼
 Answer + the SQL that ran + citations                 (FastAPI / MCP)
```

---

## 3. AWS components and exactly how each is used

### Ingestion

| Service | How this project uses it |
|---|---|
| **Kinesis Data Streams** (on-demand) | The front door. The producer calls `put_records` in batches of ≤500, partition-keyed by `customer_id` so each customer's events stay in order — which is what makes per-customer velocity features meaningful downstream. On-demand mode means no shard-count decision. Rejected alternative: MSK (per-cluster hourly rate regardless of traffic). |
| **Kinesis Firehose** | Zero-code landing. Buffers the stream (5 MB or 300 s, whichever first), GZIPs, and writes to `raw/transactions/dt=YYYY-MM-DD/` — Hive-style partitioning so Athena and Glue can prune by date without a crawler. Deliberately writes JSON, not Parquet: Parquet conversion requires a fixed schema at the landing zone, which would break the schema-evolution demo. |
| **S3** | One bucket, prefixes as zones: `raw/` (30-day expiry), `bronze/ silver/ gold/` (Iceberg under `warehouse/`), `quarantine/`, `policies/`, `athena-results/` (7-day expiry). Versioned, SSE-S3 encrypted, TLS-only bucket policy, lifecycle rules that also clean up failed multipart uploads and expired Iceberg snapshots. |

### Transformation

| Service | How this project uses it |
|---|---|
| **AWS Glue 5.x (PySpark)** | Four jobs, all capped at 2×G.1X workers / 15-min timeout. The transforms live in `glue/transforms.py` as *pure DataFrame functions* with no `awsglue` import — testable with local pytest in seconds instead of cloud runs in minutes. Job scripts are thin wrappers that read, call those functions, and write. |
| **Apache Iceberg** | The table format for bronze/silver/gold. Three properties do real work here: **MERGE INTO** makes re-runs idempotent (at-least-once delivery means every batch eventually processes twice), **snapshots** give time-travel queries, and **additive schema evolution** absorbs the producer's v2 field without rewriting history. |
| **Glue Data Catalog** | The metastore. Iceberg tables self-register through the catalog integration — no crawler, no Hive metastore server. The raw zone gets an external table with *partition projection* (partitions computed at query time, free) instead of a scheduled crawler (billed per run). |
| **Glue Data Quality (DQDL)** | The gates. Three rulesets: bronze checks structural trust (completeness, `IsUnique transaction_id`, ranges, referential integrity against the merchant dimension, freshness, `RowCount > 0`); silver checks the *features computed* (a 100%-NULL feature column passes every bronze rule and is still broken); gold checks the aggregates are sane (percentages within 0–100, unique grain). Thresholds are imported from the same constants the bronze validator uses, so the two can never disagree. |

### Orchestration

| Service | How this project uses it |
|---|---|
| **Step Functions** (Standard) | The conductor. `bronze → gate → silver → gate → gold → KB refresh`, with retries (exponential backoff on Glue transient errors), catch blocks per stage, and a Fail state whose cause names the failed stage. Key design: the quality gate **does not fail the Glue task** — a Lambda reads the verdict and a Choice state branches, so a caught bad batch renders as a visible fork (the system working), not a red error (the system broken). |
| **EventBridge Scheduler** | Hourly trigger, **disabled by default** — an armed schedule runs Glue every hour over possibly-zero rows. Flexible 15-min window. |
| **Lambda** (×4, 128–256 MB, ≤60 s) | Trigger and glue logic only, never processing: `s3_trigger` (starts the pipeline on raw arrival, with prefix filtering so the pipeline can't trigger on its own output, and idempotent execution names so one Firehose buffer window = one run), `quality_gate` (reads the verdict JSON from S3, fails closed if missing), `failure_report` (writes the incident report, publishes to SNS), `kb_refresh` (re-ingests the policy corpus; non-fatal). |
| **SNS + CloudWatch alarms** | Email on: execution failed, either quality gate failed, Glue job failed (via EventBridge rule carrying the actual error message), and pipeline freshness (no bronze write in 3 h — with `treat_missing_data = breaching`, because "the pipeline stopped entirely" produces no datapoints at all). |

### Agent layer

| Service | How this project uses it |
|---|---|
| **Bedrock (Converse API)** | On-demand only, two model tiers: Haiku for routing and SQL generation (structured, low-creativity), Sonnet only for final synthesis. Every call caps `max_tokens`; temperature 0 for routing/SQL so failures reproduce. Token usage per invocation is logged to CloudWatch. |
| **Bedrock Knowledge Bases on S3 Vectors** | Retrieval over `policies/` (3 synthetic fraud-policy docs). S3 Vectors is per-request — the decisive fact is that OpenSearch Serverless Classic idles at ~$175+/month *and deleting the KB does not delete its collection*. Titan V2 embeddings (1024-dim), fixed-size chunks with 20% overlap so threshold tables survive chunk boundaries. Answers carry `[n]` citations mapped back to sources in code, where they can't be hallucinated. |
| **Bedrock Guardrails** | Input: prompt-attack filter (HIGH). Output: PII **anonymization** (card numbers, SSN, names…) plus a regex for PAN-shaped digit runs — masked, not blocked, so answers stay useful. A denied topic blocks individual-cardholder profiling. |
| **LangGraph** | The supervisor: `route_query → call_tool → (loop) → synthesize`, with `MAX_ITERATIONS = 5` enforced *inside the routing function* — hitting the cap degrades to a partial answer with a note, instead of raising. Tool errors become state the next routing turn can react to, never crashes. |
| **The SQL validator** (`sqlglot`) | The security boundary between the model and Athena. Parses to an AST and enforces: exactly one statement, SELECT-only anywhere in the tree, every table reference (CTEs, subqueries, unions included) inside the gold allowlist, LIMIT injected/clamped. Fails closed on anything unparseable. 52 adversarial tests. Second line of defense: the agent's IAM role physically cannot read silver/bronze data. |
| **Athena** | Executes the validated SQL in the `fraud-lake` workgroup, whose **1 GB per-query scan cap is enforced server-side** — a runaway query fails instead of billing. Results include bytes-scanned, which the API returns so every answer is auditable. |

### Serving & platform

| Service | How this project uses it |
|---|---|
| **FastAPI** | `/ask` (full answer envelope: answer + SQL executed + citations + token usage + trace), `/ask/stream` (SSE progress), `/sql/check` (the validator as a demoable endpoint), `/health` (deliberately never calls Bedrock — an ALB probing a model-invoking health check is ~86k billed calls/month). |
| **MCP server** | The same three tools over the Model Context Protocol, so Claude Desktop or any MCP client can use the lakehouse directly. One implementation, three transports — the validator stays the single chokepoint. |
| **ECR + ECS Fargate** | Multi-stage, non-root, read-only-rootfs images on ARM64 (~20% cheaper). `desired_count = 0` is the steady state; ALB is opt-in because it bills ~$16/mo independent of task count. |
| **VPC** | Two modes, no NAT in either: `public_tasks` ($0 — tasks in public subnets, inbound locked to the ALB, egress via free internet gateway) or `endpoints` (~$130/mo — private subnets + 10 interface endpoints; the production shape, dearer than the NAT it replaces at this scale, torn down same-day). Free S3 gateway endpoint always on. |
| **EKS** (separate workspace) | Exists for the resume line: deployment/service/HPA via kustomize, Pod Identity reusing the same agent IAM role so the app is byte-identical to Fargate. ~$0.14/hr from creation; separate Terraform state so its teardown is an explicit act. |
| **Lake Formation** | Two personas over gold: `analyst` (column-level EXCLUDE on customer-count/risk-score columns — survives `SELECT *`) and `risk_analyst` (full gold). Enforced at the catalog, not by query convention. |
| **CloudTrail** | One trail, S3 data events scoped to the lake bucket only (account-wide data events are the expensive mistake). |
| **CloudWatch dashboard** | One pane: rows per layer, quarantine rate, DQ pass rate, Step Functions outcomes, Glue durations, agent p50/p99, tokens, tool-call mix, iterations vs the MAX_ITERATIONS line. Pipeline and agent together because "stale answer" and "pipeline didn't run" are the same incident. |
| **Terraform** | 9 modules + bootstrap + two envs. Every cost-bearing resource is behind a flag defaulting to off/cheap; provider `default_tags` stamps `Project/Env/CostCenter` on everything so Cost Explorer can attribute spend. |
| **GitHub Actions** | lint → pytest (incl. the adversarial SQL suite, run separately so a security regression is unmissable) → terraform fmt/validate/plan → ARM64 image build+push → ECS task-definition update (deliberately does *not* raise desired_count). Auth via OIDC — no long-lived AWS keys in repo secrets. |

---

## 4. The dataset

### What we use: the built-in synthetic generator (recommended)

`ingestion/producer.py` + `generator.py` produce the dataset. **This is a deliberate
design choice, not a placeholder** — for a *streaming* fraud platform, a synthetic
generator is strictly better than any public dataset:

1. **Public fraud datasets are batch CSVs; this is a streaming system.** You'd be
   replaying a static file anyway — at which point you're already generating.
2. **Ground truth you control.** Every fraudulent event is labeled with *which archetype*
   was injected (`anomaly_type`), so the silver features can be **scored** (the pipeline
   emits `FraudSignalRecallPct` — "the features catch X% of what I injected"), not just
   computed. No public dataset tells you *why* a row is fraud.
3. **The demos need knobs.** `--corrupt-rate` (quarantine demo), `--schema-version 2`
   (Iceberg evolution demo), `--replay` (dedupe demo) — impossible with a fixed file.
4. **No PII, no license questions** in a public portfolio repo.

### Event schema (the raw contract)

| Field | Type | Notes |
|---|---|---|
| `transaction_id` | UUID | Dedupe/merge key |
| `customer_id` | `CUST0000000` | 2,000-customer pool; partition key into Kinesis |
| `merchant_id` | `MER000000` | 500-merchant dimension with MCC + risk tier |
| `mcc` | string | 17 merchant categories, realistic frequency + amount distributions (grocery frequent/small; jewelry rare/large) |
| `amount` | decimal | Log-normal per MCC × per-customer spend factor |
| `currency` | string | 97% USD |
| `timestamp` | ISO-8601 UTC | Event time |
| `lat`, `lon` | double | Jittered around 16 US metro areas (80% of spend in the customer's home metro — so geography carries signal) |
| `device_id` | string | Customers have a primary device; 3% legitimate device churn |
| `channel` | enum | card_present / ecommerce / contactless / recurring; fraud skews ecommerce |
| `is_fraud` | bool | Ground-truth label (default rate 1.5%) |
| `auth_response_code` | string | **Only with `--schema-version 2`** — the evolution demo |
| `ingest_timestamp`, `schema_version`, `anomaly_type` | | Operational fields |

### The three fraud archetypes — each matched to a silver feature

| Injected pattern | What it looks like | Caught by |
|---|---|---|
| **Velocity burst** | 4–9 transactions in ~6 min, one customer, one compromised device, small "card-test" probes then escalating amounts, high-risk merchants | `txn_count_1h/24h` window counts → `is_high_velocity` |
| **Impossible geography** | A transaction from Lagos/Moscow/Jakarta minutes after a domestic one | `geo_distance_from_prior_km` + `implied_speed_kmh > 900` → `is_impossible_travel` |
| **Amount outlier** | 8–30× the customer's typical spend at a high-risk merchant | z-score vs the customer's trailing-30d mean/stddev → `is_amount_outlier` |

The generator and the feature layer are designed as a matched pair — that's what makes
the platform *evaluable*.

### If you want real-world data anyway

Adapters would map any of these to the same raw contract (replay via `--replay` after a
one-off conversion): **IEEE-CIS Fraud Detection** (Kaggle, ~590k rows, anonymized
features — good scale, but opaque columns), **ULB Credit Card Fraud** (Kaggle,
PCA-anonymized — unusable for the geo/velocity story), **Sparkov / PaySim** (also
synthetic — at which point the built-in generator with controllable anomalies wins).
Fine as a "week 2" extension; not worth blocking the build on.

### Layer-by-layer data model

- **raw** — JSON as-sent, all-strings view, partitioned by `dt`. Schema-agnostic on
  purpose; bad data is *expected* here.
- **bronze** (`fraud_bronze.transactions`) — typed, deduplicated (latest by
  `ingest_timestamp`), invalid records diverted to `quarantine/` with every
  `rejection_reason` they earned. First place schema is enforced.
- **silver** (`fraud_silver.transactions` + `merchant_dim`) — bronze + the fraud
  features above + smoothed merchant risk score + `fraud_signal_count` (0–4 composite).
- **gold** (`fraud_gold.fraud_metrics_daily`, `merchant_risk`) — aggregates by
  dt/mcc/channel and per-merchant rollups. Column names (`fraud_rate_pct`,
  `fraud_loss_amount_usd`) are deliberately verbose: they are the SQL agent's prompt
  surface, and its accuracy depends on them.

---

## 5. Where to look when something is wrong

| Symptom | First place to look |
|---|---|
| No data in S3 after seeding | Firehose monitoring tab (delivery errors), then its CloudWatch log group |
| Pipeline red in Step Functions | Click the failed state → its input/output; the Fail cause names the stage |
| Gate keeps failing | `quality-reports/<layer>/report.json` in S3, then the quarantine breakdown |
| Agent gives a wrong number | The `sql_executed` field in the API response — check the query, not the prose |
| Bill higher than expected | Cost Explorer grouped by service; hunt hourly floors: EKS, ALB, endpoints, Kinesis stream |
