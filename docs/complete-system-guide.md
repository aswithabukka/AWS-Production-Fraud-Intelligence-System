# The Complete System Guide

*What we built, every AWS service in it, why each one is there, how they connect, and
when you'd use each of them again.*

Read this top to bottom once and you should be able to explain the whole platform to
someone else — which is the real test of understanding it.

---

## 1. The Big Picture — What Is This Thing?

We built a **streaming lakehouse with an AI layer** for card-transaction fraud analytics:

- **Streaming** — transactions arrive continuously as events, not as nightly file drops
- **Lakehouse** — data lives cheaply in S3 like a *data lake*, but behaves like a
  *warehouse*: typed tables, SQL, transactions, time travel
- **AI layer** — two kinds: classic **ML models** that score each transaction for fraud,
  and an **LLM agent** that lets anyone ask questions in English and get answers backed
  by real SQL and cited documents

```
                        ┌─────────────────  THE DATA PLANE  ─────────────────┐
 producer ──▶ Kinesis ──▶ Firehose ──▶ S3 raw/ ──▶ bronze ──▶ silver ──▶ gold
 (events)    (stream)    (lander)     (files)     (typed)   (features)  (answers)
                                                     │                    │
                                                 quarantine          ML ensemble
                                                 (bad rows)         (fraud scores)

                        ┌──────────────  THE CONTROL PLANE  ─────────────────┐
     EventBridge / S3-event ──▶ Step Functions ──▶ Glue jobs + quality gates
                                     │
                              SNS alerts + CloudWatch alarms/dashboard

                        ┌───────────  THE INTELLIGENCE PLANE  ───────────────┐
     you ──▶ FastAPI/MCP ──▶ LangGraph agent ──▶ Bedrock (Claude models)
                                  │── SQL tool ──▶ validator ──▶ Athena ──▶ gold
                                  │── policy tool ──▶ Knowledge Base ──▶ S3 Vectors
                                  └── ops tool ──▶ Step Functions history

                        ┌───────────  THE GOVERNANCE PLANE  ─────────────────┐
     IAM (who may call APIs) · Lake Formation (who may see which columns)
     CloudTrail (who did what) · Budgets (what it costs)
```

**Why anyone builds this:** a company receiving events (payments, clicks, sensor
readings, orders) needs to (a) keep them forever cheaply, (b) clean and enrich them
reliably, (c) let analysts and models consume them, and (d) increasingly, let
non-technical people query them in plain language. This architecture is the current
industry-standard answer to all four, which is why its skill names appear in data
engineering job descriptions verbatim.

---

## 2. The Journey of One Transaction

The best way to understand the system is to follow a single swipe of a card through it:

1. **Born** — the producer creates a JSON event: who, where, how much, which merchant.
2. **Streamed** — it's written to **Kinesis**, partitioned by customer so one customer's
   events stay in order (order is what makes "5 transactions in 10 minutes" detectable).
3. **Landed** — **Firehose** buffers a few thousand events and writes them as one
   compressed file to **S3** under `raw/transactions/dt=2026-08-15/`. Raw is immutable
   history: never edited, only appended, replayable forever.
4. **Triggered** — the S3 arrival fires a **Lambda**, which starts one **Step Functions**
   execution (duplicate triggers collapse into one run via idempotent naming).
5. **Typed** — the **bronze Glue job** parses the JSON, enforces types, rejects broken
   records to `quarantine/` *with the reason attached*, deduplicates, and MERGEs into an
   **Iceberg** table. From here on, everything downstream can trust the schema.
6. **Gated** — **Glue Data Quality** evaluates ~14 rules (uniqueness, ranges,
   referential integrity, freshness). A Lambda reads the verdict; a Choice state
   branches. Fail → downstream is *skipped* (stale beats wrong) and **SNS** emails you.
7. **Enriched** — the **silver job** computes fraud features with window functions:
   velocity counts, amount z-score vs. the customer's own history, geo-distance and
   implied travel speed, device changes, merchant risk.
8. **Aggregated** — the **gold job** rolls silver into the tables people actually query:
   daily fraud metrics by category/channel, per-merchant risk.
9. **Scored** — the **ML job** trains six models on silver features (LightGBM, XGBoost,
   Random Forest, SVM, Isolation Forest, autoencoder), averages them into one
   `ensemble_fraud_score` per transaction, and writes scores + honest holdout metrics
   back to gold.
10. **Asked about** — you type *"which merchant category has the highest fraud loss?"*
    into the console. The **LangGraph agent** routes to its SQL tool, a small **Claude**
    model writes SQL, the **validator** proves it's a single read-only SELECT on
    allowlisted tables, **Athena** runs it against gold, and a larger Claude model writes
    the answer — with the SQL attached so you can check it.

Every stage is restartable, idempotent, and observable. That's the whole design in one
sentence.

---

## 3. Service by Service — What, Why, and When You'd Use It Again

Each entry: what the service *is* in general → the job it does *here* → what it
connects to → **when to reach for it in your future work**.

### Storage & Table Layer

#### Amazon S3
- **What it is:** infinitely scalable object storage — files with URLs, eleven 9s of
  durability, pennies per GB.
- **Here:** the single home of ALL data — raw JSON, Iceberg tables, quarantine, policy
  PDFs, quality reports, Terraform state, Athena results. One bucket, prefixes as zones.
- **Connects to:** literally everything; S3 is the hub every other service reads/writes.
- **Future use:** the default answer for storing anything that is a file. Data lakes,
  backups, static websites, model artifacts, logs. If data doesn't need millisecond
  lookup by key, it probably belongs in S3.
- **Watch for:** lifecycle rules (we expire raw/ at 30 days) — unbounded buckets grow
  silently forever.

#### Apache Iceberg *(open table format, not an AWS service)*
- **What it is:** a metadata layer that turns "a pile of Parquet files in S3" into a
  real table: ACID transactions, schema evolution, snapshots, time travel, row-level
  DELETE/MERGE.
- **Here:** bronze/silver/gold are all Iceberg. It gave us three live demos: adding a
  column with zero downtime, querying the table *as it was* before a change, and
  deleting 146 poisoned rows with one SQL statement.
- **Connects to:** written by Glue Spark jobs, registered in the Glue Data Catalog,
  queried by Athena.
- **Future use:** any time multiple engines must read/write the same S3-based tables
  safely, or you need UPDATE/DELETE/MERGE on a lake, or auditors ask "what did the data
  look like last Tuesday?" Competitors: Delta Lake (Databricks-centric), Hudi. Iceberg
  is the most engine-neutral and the AWS-native pick.

#### AWS Glue Data Catalog
- **What it is:** a serverless metastore — the "phone book" that maps table names to
  schemas and S3 locations, shared by every engine.
- **Here:** four databases (`fraud_raw/bronze/silver/gold`). Iceberg self-registers its
  tables; Athena and the agent's schema introspection both read from it. We deliberately
  used **partition projection** on the raw table instead of paying for scheduled
  crawlers.
- **Future use:** you get it implicitly whenever you use Athena/Glue/EMR on AWS. The
  lesson that transfers: *one shared catalog* is what prevents every team from
  maintaining their own contradictory idea of the schema.

### Ingestion Layer

#### Amazon Kinesis Data Streams
- **What it is:** a managed, ordered, replayable event stream — AWS's Kafka equivalent.
  Producers put records; consumers read them in order per partition key.
- **Here:** the front door. Partition key = `customer_id`, which guarantees per-customer
  ordering — the property the velocity features depend on.
- **Connects to:** producer → Kinesis → Firehose.
- **Future use:** whenever events must be *reacted to* or *fanned out* in near-real-time:
  clickstreams, IoT, CDC feeds, live leaderboards. Choose **on-demand mode** until you
  can prove steady throughput.
- **Why not Kafka/MSK:** MSK bills per-cluster-hour whether or not traffic flows
  (~hundreds/month floor). At small scale, Kinesis's per-request shape wins; at massive
  scale with existing Kafka expertise, MSK becomes defensible. Being able to argue this
  trade-off *is* the senior-engineer answer.
- **Watch for:** even on-demand Kinesis has a small per-stream-hour charge (~$29/mo if
  left up idle) — hence our `make stream-down` habit.

#### Amazon Data Firehose
- **What it is:** a zero-code "lander": buffers a stream and delivers it to S3 (or
  Redshift/OpenSearch/HTTP) with batching, compression, and partitioned paths.
- **Here:** Kinesis → 5 MB/300 s buffers → GZIP JSON → `raw/transactions/dt=…/`. The
  `dt=` path pattern is what lets every downstream engine prune by date for free.
- **Future use:** any "stream → files" hop where you'd otherwise write consumer code.
  The buffering question it answers — *latency vs. file count* — exists in every
  streaming system: small buffers = fresh data but thousands of tiny files that choke
  Spark; big buffers = fewer files but staler data.

### Processing Layer

#### AWS Glue (Spark jobs)
- **What it is:** serverless Apache Spark — you submit a PySpark script, AWS provisions
  workers, runs it, bills per second, tears down. No cluster to manage.
- **Here:** five jobs (bronze/silver/gold/quality/ml), all capped at 2 small workers and
  15 minutes. The transforms are *pure functions* tested locally — the Glue scripts are
  thin wrappers. That separation is why we could test in seconds what takes minutes to
  run in the cloud.
- **Connects to:** reads/writes S3+Iceberg via the Catalog; orchestrated by Step
  Functions; logs and metrics to CloudWatch.
- **Future use:** batch/micro-batch transformation of data too big for pandas but not
  worth a permanent cluster. If your data fits in pandas, use Lambda or a container; if
  you need a long-lived tuned cluster, that's EMR.
- **Hard-won lessons:** Glue ≈ Spark but not = (static configs before the session,
  `F.nullif` unsupported, sklearn absent from the Spark runtime) — always expect one
  round of cloud-only debugging after local tests pass.

#### AWS Glue Data Quality (DQDL)
- **What it is:** a rules language + engine that scores a dataset against declarative
  expectations (completeness, uniqueness, ranges, freshness, custom SQL).
- **Here:** the *gates* between layers. Bronze rules ask "is this structurally
  trustworthy?", silver rules ask "did the feature computation actually work?" A failing
  gate halts the pipeline — gold goes stale, never wrong.
- **Future use:** any pipeline where bad data downstream is worse than no data
  downstream (that's most pipelines). The transferable design: *quality checks as
  gates with teeth*, not dashboards nobody reads. Alternatives: Great Expectations,
  dbt tests, Soda.

#### AWS Lambda
- **What it is:** run a function on demand, millisecond billing, zero servers.
- **Here:** four small functions, all "trigger and glue" — start the pipeline on S3
  arrival, read a quality verdict, write a failure report, kick a KB re-index. The rule
  we enforced: **never heavy processing in Lambda** — 15-minute limit, memory limits,
  and cost make it wrong for data crunching.
- **Future use:** event reactions, API backends, glue between services, cron-style
  chores. The default compute for anything under a minute and under a GB.

### Orchestration Layer

#### AWS Step Functions
- **What it is:** a state-machine orchestrator: define steps, retries, branches, and
  error handling as JSON; get a visual execution graph and full history.
- **Here:** the conductor — bronze → gate → silver → gate → gold → ML, with exponential
  backoff retries, per-stage catch blocks, a failure-report path, and the quality gates
  as *visible forks*. The graph view IS the mental model of this backend.
- **Connects to:** invokes Glue and Lambda; started by EventBridge/S3 events; its
  history is read by the agent's `pipeline_status` tool.
- **Future use:** multi-step workflows with failure semantics — order processing, data
  pipelines, human-approval flows. If your process has "if X fails, do Y, then alert Z",
  reach for it.
- **Why not Airflow:** managed Airflow (MWAA) is an always-on environment
  (~$350+/month). Step Functions is per-transition (free tier covered this project).
  Airflow wins when you need complex scheduling semantics, big DAG ecosystems, or
  backfills as a first-class concept.

#### Amazon EventBridge Scheduler
- **What it is:** serverless cron — invoke almost any AWS API on a schedule.
- **Here:** the hourly pipeline trigger, **disabled by default** because an armed
  schedule runs Glue every hour over possibly-zero new rows.
- **Future use:** any recurring task; also EventBridge *rules* (the sibling feature)
  route events between services — we used one to alert on Glue job state changes.

#### Amazon SNS
- **What it is:** publish/subscribe messaging — publish once, deliver to email, SMS,
  Lambda, queues.
- **Here:** the alert channel. Quality-gate failures, pipeline failures, and alarms all
  publish to one topic that emails you.
- **Future use:** whenever "something happened, tell N interested parties." Pairs with
  SQS (queues) when consumers need durability and retry.

### Analytics Layer

#### Amazon Athena
- **What it is:** serverless SQL directly over S3 — no cluster, no loading; pay per TB
  scanned.
- **Here:** the query engine for everything — your console queries, the agent's
  generated SQL, the failure-scenario demos, even row-level DELETEs on Iceberg. Guarded
  by a workgroup with a **1 GB per-query scan cap** enforced server-side.
- **Future use:** ad-hoc and BI-style SQL over lake data, log analysis, quick
  exploration of any S3 dataset. The pricing shape means an idle analytics stack costs
  $0 — revolutionary vs. warehouse clusters.
- **Why not Redshift:** Redshift (even Serverless) has capacity floors and shines at
  high-concurrency dashboard workloads over huge data. For "a few GB, queried
  occasionally," Athena is strictly better.

### AI / Intelligence Layer

#### Amazon Bedrock (foundation models via Converse API)
- **What it is:** serverless access to foundation models (Claude, Titan, etc.) — no
  GPUs, no endpoints to manage, pay per token.
- **Here:** three models with deliberate tiering: **Claude Haiku** for routing and SQL
  generation (structured, cheap, fast), **Claude Sonnet** only for final answer
  synthesis, **Titan Embeddings** for the knowledge base. Every call caps `max_tokens`;
  the agent loop hard-stops at 5 iterations.
- **Future use:** any LLM feature inside an AWS-first company — chatbots, extraction,
  summarization, agents. The transferable pattern is the tiering: *small model for
  structure, large model for prose* — the single easiest 5–10x cost saver in LLM apps.
- **Gotchas we hit:** Anthropic models need a one-time use-case form per account; model
  IAM should name exact model ARNs so a config change can't silently 20x your per-token
  cost.

#### Bedrock Knowledge Bases + S3 Vectors
- **What it is:** managed RAG — point it at documents in S3; it chunks, embeds, and
  indexes them; you call `retrieve()` and get relevant passages with sources. S3 Vectors
  is the vector store: embeddings in S3, per-request pricing, **no idle compute**.
- **Here:** the fraud-policy corpus (chargeback tiers, monitoring thresholds). The
  agent's `search_policies` tool retrieves passages and the answer cites them `[1]` —
  a policy answer without a citation is just an assertion.
- **Future use:** "chat with our documents" features. The vector-store decision is the
  cost decision: OpenSearch Serverless idles at ~$175+/month *and famously outlives the
  KB that created it*; S3 Vectors idles at ~$0. At huge scale/low latency, dedicated
  vector DBs earn their keep — not before.
- **Gotcha we hit:** S3 Vectors caps *filterable* metadata at 2 KB/vector and Bedrock
  stores chunk text in metadata — every `AMAZON_BEDROCK_TEXT*` key must be declared
  non-filterable or real documents fail ingestion.

#### Bedrock Guardrails
- **What it is:** a policy filter wrapped around model calls: PII masking, prompt-attack
  detection, topic denial.
- **Here:** inputs screened for prompt injection; outputs anonymize card numbers, SSNs,
  names (masking, not blocking — a fraud tool that blocks every answer touching an
  identifier is useless); a denied topic blocks individual-cardholder profiling.
- **Future use:** any user-facing LLM feature. But remember the deeper lesson from our
  SQL validator: guardrails are *one* layer — the real security control is
  deterministic code (parse the SQL; scope the IAM) that doesn't rely on a model
  behaving.

#### The pieces we built ourselves (the differentiators)
- **SQL validator** (`sqlglot` AST): treats the LLM as untrusted input. Exactly one
  statement, SELECT-only anywhere in the tree, gold-tables-only including inside CTEs
  and UNIONs, LIMIT injected. 52 adversarial tests. *Pattern to reuse: never execute
  model-generated anything without a parser-level check.*
- **LangGraph supervisor**: explicit route → tool → synthesize loop with the iteration
  cap enforced in code. *Pattern to reuse: when someone asks "what if the model loops
  forever?", the answer should be a line of your code.*
- **ML ensemble**: six diverse models averaged; unsupervised members keep signal even if
  labels lie; leakage guards throughout; metrics written to a queryable table. *Pattern
  to reuse: models are pipeline stages with the same idempotency and observability
  duties as any other job.*

### Serving Layer

#### ECR + ECS Fargate (+ ALB)
- **What they are:** ECR = private Docker registry. Fargate = run containers without
  managing servers, per-second billing. ALB = managed load balancer.
- **Here:** the FastAPI console and MCP server are containerized (multi-stage, non-root,
  ARM64 for ~20% cheaper compute) with `desired_count = 0` at rest — the service exists,
  costs nothing, and a demo is one variable away. The ALB is opt-in because it bills
  ~$16/month from the moment it exists *regardless of task count* — the classic trap.
- **Future use:** Fargate is the default for containerized services without cluster
  ops. The reusable judgment: know which pieces bill-at-rest (ALB, NAT) vs.
  scale-to-zero (tasks).

#### Amazon EKS *(built, deliberately not left running)*
- **What it is:** managed Kubernetes.
- **Here:** a separate Terraform workspace with its own state, because the control plane
  bills ~$73/month from the moment it exists. Manifests (Deployment/Service/HPA + Pod
  Identity reusing the same IAM role as Fargate) are ready for a same-day demo.
- **Future use:** EKS earns its floor when you need the Kubernetes *ecosystem* —
  Helm charts, operators, multi-team clusters, portability requirements. For "run my
  container," Fargate at $0-at-rest wins. Knowing *when not to use Kubernetes* is the
  interview answer.

### Governance & Safety Layer

#### AWS IAM
- **What it is:** who (users/roles) may call which APIs on which resources.
- **Here:** one narrow role per component — Firehose can read one stream and write one
  prefix; the agent can query one workgroup and read gold only; nobody has `*`. The
  human operator is broad; the machine roles are narrow — that asymmetry is deliberate
  and defensible.
- **Future use:** every AWS project, day one. The transferable habit: write the policy
  from "what does this component actually do?" not from "what makes the error go away."

#### AWS Lake Formation
- **What it is:** data-level permissions on top of IAM — table- and **column-level**
  grants enforced by the catalog, with credential vending for S3 access.
- **Here:** two personas prove it: `analyst` runs `SELECT *` and gets 14 columns;
  `risk_analyst` runs the identical query and gets 16. The excluded columns don't
  exist for the analyst, no matter what SQL they write.
- **Future use:** multi-team lakes where different roles may see different slices of the
  same tables — the compliance answer for PII columns.
- **The five lessons it taught us** (all in the field guide): IAM admin ≠ LF admin; the
  grandfathered allow-everyone default shadows your grants until revoked per table;
  grant the pipeline explicitly *before* revoking defaults; register the prefix where
  data actually lives; and even the administrator needs an explicit SELECT — *admins
  govern, access is always explicit.*

#### AWS CloudTrail
- **What it is:** the audit log — every API call, by whom, from where.
- **Here:** one trail, with S3 *data events* scoped to the lake bucket only (account-wide
  data events are the classic expensive mistake — every Athena result write becomes a
  billable event).
- **Future use:** on by default in any account you care about; required for any
  compliance conversation.

#### CloudWatch (metrics, logs, alarms, dashboard)
- **What it is:** AWS's built-in observability: metric time series, log storage/search,
  threshold alarms, dashboards.
- **Here:** every job emits custom metrics (rows written, quarantine rate, DQ pass rate,
  fraud-signal recall, ensemble AUC, agent tokens/latency); five alarms email on
  failure or staleness — including the subtle one: freshness treats *missing data as
  breaching*, because "the pipeline stopped entirely" emits nothing at all; ONE dashboard
  puts pipeline and agent on the same pane, because "the agent gave a stale answer" and
  "the pipeline didn't run" are the same incident.
- **Future use:** every AWS workload. The reusable idea is *the system reports its own
  health as data* — logs are for humans investigating; metrics are for machines
  deciding.

### Foundations

#### Terraform (+ S3/DynamoDB remote state)
- **What it is:** infrastructure as code — declare resources in files; `plan` shows the
  diff; `apply` makes reality match.
- **Here:** ~90 resources across 9 modules and 3 workspaces, every cost-bearing item
  behind a flag defaulting to cheap, tags applied globally so Cost Explorer can
  attribute every cent. Remote state in S3 + a DynamoDB lock so state survives laptops.
- **Future use:** any infrastructure you'll touch twice. Habits that transfer: *always
  read the plan* (our reviews caught the stream resurrecting itself), plans are
  single-use, state surgery (`state rm`) is a legitimate tool.

#### GitHub Actions (CI/CD)
- **Here:** lint → 145 local tests (the adversarial SQL suite runs separately so a
  security regression is unmissable) → terraform validate → ARM64 image build → deploy,
  authenticated via **OIDC** so no long-lived AWS keys sit in repo secrets.
- **Future use:** every repo. OIDC-instead-of-keys is the pattern interviewers probe.

---

## 4. Why This *Specific* Shape — The Five Principles

Strip away the service names and the architecture is five decisions. These transfer to
every data platform you'll ever build:

1. **Sort every service by pricing shape before using it.** Per-request (safe) /
  scales-to-zero (verify) / hourly floor (needs justification + teardown plan). This
  single habit is why two weeks of building cost ~$3. It's also why we rejected MSK,
  MWAA, OpenSearch, Redshift, and NAT gateways — each fails the shape test at this
  scale, and each rejection is an interview answer.

2. **Medallion layering (raw → bronze → silver → gold).** Each layer has one job and one
  guarantee: raw = immutable history, bronze = typed and deduplicated, silver =
  enriched, gold = consumable answers. Corollary we proved live: *fixes flow the same
  direction as data* — cleaning bronze while raw stayed poisoned just re-imported the
  poison.

3. **Gates fail closed; downstream stays stale, never wrong.** Bad data halts promotion
  and alerts a human. An empty run is a failure (`RowCount > 0`), because a green
  pipeline over zero rows is the worst outcome — nothing alerts.

4. **Idempotency everywhere, because at-least-once is reality.** Kinesis redelivers,
  Step Functions retries, humans re-run. Every write is MERGE-by-key or
  overwrite-by-partition; we delivered the identical batch twice and row counts held to
  the digit.

5. **Deterministic code guards probabilistic components.** The LLM proposes; the AST
  validator disposes. The models score; leakage guards and holdout metrics keep the
  scores honest. IAM is the second wall behind every first wall.

---

## 5. "When Should I Use X?" — The Future-Scenarios Cheat Sheet

| You're asked to… | Reach for | Because |
|---|---|---|
| Store files/data cheaply forever | S3 (+ lifecycle rules) | The default substrate of AWS |
| React to events in near-real-time | Kinesis (+ Lambda or Firehose) | Ordered, replayable, per-request |
| Get streaming data into files with no code | Firehose | Buffering/partitioning solved for you |
| Transform data too big for pandas, no cluster wanted | Glue Spark jobs | Serverless Spark, per-second billing |
| Give lake tables ACID/UPDATE/time-travel | Iceberg | Engine-neutral, AWS-native support |
| SQL over S3 without loading anything | Athena | Pay per query; $0 idle |
| Orchestrate multi-step work with retries/branching | Step Functions | Failure handling as config + visual history |
| Small glue logic between services | Lambda | Millisecond billing, zero servers |
| Stop bad data from propagating | Glue DQ (or dbt tests/GE) as a *gate* | Checks with teeth, not dashboards |
| Add an LLM feature in an AWS shop | Bedrock, tiered models, capped tokens | Serverless models; tiering is the cost lever |
| "Chat with our documents" | Bedrock KB + S3 Vectors | Managed RAG with no idle vector bill |
| Let an LLM touch a database | A parser-level validator + scoped IAM | Never trust generated code without proof |
| Score events with ML in a pipeline | Ensemble as a pipeline stage, metrics to a table | Models are jobs with the same duties as ETL |
| Different people see different columns | Lake Formation | Enforced at the catalog, survives SELECT * |
| Run a container cheaply | Fargate, desired_count 0 at rest | Kubernetes only when you need its ecosystem |
| Any infrastructure at all | Terraform, cost flags default-off | Read the plan. Every time. |

---

## 6. Where Everything Lives

| | |
|---|---|
| The measured results of every live run | `docs/as-run-results.md` |
| All 30 issues + fixes + lessons | `docs/troubleshooting-guide.html` |
| Why each design decision beat its alternative | `docs/decisions.md` (D-001…D-012) |
| How this project's internals work | `docs/how-it-works.md` |
| AWS-from-zero setup walkthrough | `docs/aws-getting-started.md` |
| Reproducible failure drills | `docs/failure-scenarios.md` |
| Project rules & cost guardrails | `docs/PROJECT_RULES.md` |

*One honest closing note, consistent with everything in this repo: the data is
synthetic and its features were engineered to catch the injected fraud patterns. Every
skill here is real; the AUC numbers describe the demo, not production. Saying that
unprompted is worth more than the numbers.*
