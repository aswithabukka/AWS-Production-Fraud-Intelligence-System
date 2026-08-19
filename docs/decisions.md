# Design decisions

Each entry is a decision, the alternative that was rejected, and the reasoning. This file is
the interview-prep artifact — if a decision can't be defended in two sentences here, it
shouldn't be in the build.

---

## D-001 — Kinesis Data Streams over MSK
**Rejected:** MSK / MSK Serverless.
Managed Kafka bills a per-cluster hourly rate regardless of traffic. At ~50 events/sec the
operational overhead of Kafka isn't justified, and the ability to say *why* it isn't
justified is the senior answer. Kinesis on-demand is per-request with a small per-stream-hour
component, and it scales to the demo without a capacity decision.

## D-002 — Firehose for landing, not a Lambda consumer
**Rejected:** custom Lambda consumer writing to S3.
Firehose handles buffering, `dt=` partitioning, compression, and (later) Parquet conversion
with no code to defend in review. A Lambda consumer would mean writing checkpointing,
batching, and retry logic that adds risk without adding a talking point.

## D-003 — Apache Iceberg as the table format
**Rejected:** Delta Lake, Hudi, plain Parquet.
Iceberg is first-class in both Glue 5.x and Athena — no extra connector, no custom classpath.
It supplies three concrete demo artifacts: schema evolution, snapshot history, and
time-travel queries. Those are slice 1c's failure scenarios, so the table format is doing
narrative work, not just storage work.

## D-004 — Athena over Redshift Serverless
**Rejected:** Redshift Serverless.
Athena is per-query with no idle cost. Redshift Serverless has a base-capacity floor that
bills whether or not a query runs. For a lakehouse whose data volume is a few GB, the query
engine should cost nothing when nobody is asking questions.

## D-005 — Step Functions over Airflow/MWAA
**Rejected:** MWAA, Dagster.
MWAA runs an always-on environment (~$350+/month). Step Functions is per-state-transition,
covered by the free tier at this volume, and produces a visual execution graph that
screenshots well for the README. The tradeoff — no rich scheduling semantics — doesn't bite
on an hourly pipeline with one branch.

## D-006 — Bedrock Knowledge Bases on **S3 Vectors**, not OpenSearch Serverless
**Rejected:** OpenSearch Serverless (Classic), Pinecone, pgvector.
This is the single most important cost decision in the project. OpenSearch Serverless
Classic has a ~2 OCU minimum — roughly $175/month for dev, $350/month for production
configuration — billed while completely idle. Worse: **deleting the Bedrock Knowledge Base
does not delete the collection it provisioned**, so it keeps billing from a console you're
no longer looking at. S3 Vectors is per-request: storage plus a per-query fee, no idle
compute. For a policy corpus of a few dozen PDFs this is the correct engineering answer as
well as the cheap one.

## D-007 — LangGraph supervisor over Bedrock Agents
**Rejected:** Bedrock Agents (managed).
Explicit graph wiring — `route_query` / `call_tool` / `synthesize`, with `MAX_ITERATIONS`
enforced inside the routing function rather than as a graph edge — is far more
interview-defensible than a managed black box. When asked "what happens if the model keeps
calling tools," the answer should be a line of code, not "the service handles it."

## D-008 — SSE-S3 by default, customer-managed KMS key opt-in
**Rejected:** always-on customer-managed KMS key (as originally specced).
A CMK carries a ~$1/month floor plus per-request charges, and the spec's own cost rule says
one key maximum. SSE-S3 (AES256) is free, encrypts at rest identically from the object's
point of view, and is not the part of the security story that carries weight in an
interview — IAM scoping and Lake Formation column-level control are. The CMK is behind
`use_kms_cmk` (default `false`): flip it on to capture the screenshot for the security
section, flip it back off. The *code path* exists and is reviewable either way, which is
what the resume bullet actually needs.

## D-009 — Kinesis stream behind an `enable_stream` flag
**New (not in the original spec).**
The Kinesis stream and Firehose are the only slice-1a resources with a meaningful idle
cost (~$0.036/stream-hour ≈ $26–29/month if left running for a month). Gating both behind
`enable_stream` means `make stream-down` removes the ingest path while leaving the S3 lake,
Glue Catalog, and Athena workgroup intact — so every gold-layer query and the entire slice-2
agent demo still work at effectively zero cost. Ingest is bursty by nature in a portfolio
project; the infrastructure should match that shape.

## D-010 — No NAT Gateway, at any point
**Rejected:** private subnets with a NAT gateway (the default VPC pattern).
A NAT gateway is ~$32/month plus per-GB processing, billed continuously, and it is the most
common way a portfolio AWS account quietly costs $40/month. Slice 3 uses VPC interface
endpoints for Glue, Athena, Bedrock, ECR, and CloudWatch Logs, plus a gateway endpoint for
S3. Gateway endpoints are free; interface endpoints have a small per-hour charge but only
exist during the Fargate demo window.

## D-011 — Firehose writes JSON in slice 1a, not Parquet
**New.**
Firehose's Parquet conversion requires a Glue table with a fixed schema at the landing zone,
which conflicts with the slice-1c schema-evolution demo (the producer emits an extra field
and the *bronze* Iceberg table evolves). Landing raw JSON keeps the raw zone
schema-agnostic — the correct role for a raw zone — and makes bronze the first place schema
is enforced. GZIP compression on the delivery stream keeps the storage cost the same order
of magnitude.

## D-012 — Model layer: five-model ensemble on Glue, not SageMaker
**Rejected:** SageMaker (spec non-goal), always-on inference endpoints, a single model.
The scoring stage trains LightGBM, XGBoost, RandomForest, an RBF-SVM, and an
IsolationForest on silver features and averages them. Diversity is the point: four
supervised learners with different inductive biases plus one unsupervised member that
needs no labels at all — so the ensemble degrades gracefully if the label is ever wrong
or missing. Training runs inside a Glue Spark job (the learning itself is sklearn on the
driver — the dataset is portfolio-scale), which keeps the pricing shape per-request and
the write path Iceberg like every other stage. Scores land in
`gold.transaction_risk_scores` and holdout metrics in `gold.model_metrics`, both on the
SQL agent's allowlist — "which model has the best AUC?" is now an answerable question.

Honesty rule attached to this layer: silver features were engineered to catch exactly
the fraud archetypes the generator injects, so holdout metrics validate the feature
engineering, not real-world fraud performance. The threshold is chosen on the training
split only; choosing it on holdout would leak the test set into the decision rule.

## D-013 — Team dashboards served by the API container, not a BI product

Three team dashboards (fraud ops, model health, business value) are hand-built pages
served by the existing FastAPI container at `/dashboards`, backed by five fixed Athena
queries with a 5-minute in-process cache.

Why not QuickSight / Managed Grafana: both bill per user per month ($9–24) for what is,
at this scale, a handful of small Athena scans and some SVG. The hand-built path keeps
the platform's pricing shape (per-request, $0 at rest), adds zero infrastructure, and
the charts follow a validated design method — the six model colors pass colorblind-
separation and contrast checks against the console surface (checked with a script, not
by eye), the ensemble is encoded by pattern rather than claiming a seventh hue, and
every chart ships a hover layer plus a table view.

The business numbers come from a new gold table, `fraud_gold.fraud_value_daily`
(confusion-matrix cells priced per day: caught/missed/false-alarm dollars), written by
the ML job — deliberately a governed table rather than dashboard-side arithmetic, so
the SQL agent can answer "how much did we save last week?" from the same source the
dashboard draws.
