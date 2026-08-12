# fraud-lake — project rules

An AWS streaming lakehouse with an agentic analytics layer, built as a portfolio project
for AI Data Engineer roles. Domain: synthetic card-transaction fraud analytics.

**Code quality and explainability matter more than feature count.** Every design decision
must be defensible in an interview. When a decision is made, write the reasoning into
`docs/decisions.md`.

---

## Architecture (locked — do not propose alternatives)

```
Kinesis Data Streams (on-demand) -> Firehose -> S3 raw/
S3 raw -> Glue 5.x PySpark -> Apache Iceberg tables on S3 (bronze / silver / gold)
Glue Data Catalog = metastore; Athena = query engine
EventBridge Scheduler -> Step Functions -> Glue jobs + data-quality gate
Bedrock + LangGraph supervisor over Athena, Bedrock Knowledge Bases (S3 Vectors),
  and Step Functions execution history
FastAPI service + MCP server, containerized, ECR, ECS Fargate day-to-day, one EKS demo
Terraform for all infrastructure; GitHub Actions for CI/CD
```

### Explicit non-goals
No MSK. No OpenSearch Serverless. No SageMaker. No Redshift. No dual RAG implementations.
**No always-on compute anywhere in the default state.**

---

## Agent guardrails (persist across sessions)

1. **Never run `terraform apply`, `aws` mutating commands, `eksctl`, or `docker push`.**
   Write the code, show the plan, stop. The human runs every apply and every console action.
   This is a cost-control rule, not a trust rule.
2. Never create a resource with an hourly floor without flagging it explicitly in the
   response and adding a row to `COSTS.md` **before** the code is written.
3. No `AdministratorAccess` and no `"Action": "*"` in any IAM policy — including for
   convenience during development. One role per component:
   `GlueRole`, `LambdaRole`, `StepFnRole`, `AgentRole`, `TaskRole`.
4. No secrets, account IDs, or ARNs hardcoded in committed files. Use variables,
   `data.aws_caller_identity`, and Secrets Manager.
5. Prefer the smallest instance/worker class that demonstrates the concept.
6. Every Terraform module must be destroyable — `make destroy` tears down everything.
7. Build in slices. Do not start slice N+1 until slice N runs end to end.

---

## Cost rules (enforce in every Terraform module)

The metric that matters is **pricing shape**, not price per unit. Sort every service into
one bucket before creating it:

| Shape | Behavior | Rule |
|---|---|---|
| Per-request | Costs nothing when idle | Safe. Create freely. |
| Per-hour, scales to zero | Free when idle *if configured right* | Verify the scale-to-zero config, then create. |
| **Per-hour floor** | Bills whether you use it or not | Needs a `COSTS.md` row, a teardown step, and a calendar reminder. |

### Hard limits
- Every resource gets tags: `Project=fraud-lake`, `Env=dev`, `CostCenter=portfolio`.
  Applied via `default_tags` on the provider — never hand-tagged.
- **Kinesis**: on-demand mode, or 1 provisioned shard. Never more.
- **Glue jobs**: `G.1X`, 2 workers max, `--enable-auto-scaling`, timeout 15 min.
- **Athena**: workgroup with a per-query data scan limit of 1 GB, enforced at the workgroup.
- **Bedrock**: on-demand invocation only. No provisioned throughput, ever.
- **Lambda**: 128–512 MB, timeout <= 60s.
- **ECS Fargate**: `desired_count` defaults to 0.
- **EKS**: created only for a demo window, destroyed the same day, in a separate
  Terraform workspace (`terraform/envs/eks-demo`).
- **No NAT Gateway, ever.** Use VPC endpoints (S3, Glue, Athena, Bedrock, ECR, Logs)
  for private subnets. A NAT gateway is the classic silent ~$32/month drain.

### Bedrock cost control
- Smallest model that works, per agent: a fast/cheap model for routing and SQL generation,
  a larger one only for final synthesis.
- Cap `max_tokens` on every call. Log token usage per invocation to CloudWatch.
- Cache Glue schema descriptions locally instead of re-sending them every turn.
- Hard iteration limit on the LangGraph loop: `MAX_ITERATIONS = 5`, enforced in the routing
  logic, not just as a graph edge.

### Target
Steady state with everything except EKS: **under ~$20–30/month**, and closer to ~$3–5/month
if the stream is torn down between demo sessions. If you cross $50, something has an hourly
floor that was not classified. Check NAT gateways, EKS, and any vector store first.

### Non-negotiable habits
1. `make destroy` must work, and it gets run whenever you're away more than a few days.
2. `COSTS.md` gets a row *before* the resource gets created, not after.
3. Check Cost Explorer every Monday, grouped by service.
4. Tag everything. Untagged spend is unattributable spend.

---

## Conventions

- **Region**: `us-east-1` only. Everything in one region.
- **AWS profile**: `fraud-lake`. Never the root account.
- **Terraform**: modules under `terraform/modules/`, environments under `terraform/envs/`.
  Remote state in S3 + DynamoDB lock (both are per-request, effectively free).
- **Python**: 3.11+, type hints on public functions, `ruff` for lint, `pytest` for tests.
- **PySpark**: window functions over `collect()`-to-driver. Every Glue job must have a
  local pytest against small fixture DataFrames so logic is testable without running Glue.
- **Naming**: S3 bucket `fraud-lake-<account-id>`; Glue databases `fraud_bronze`,
  `fraud_silver`, `fraud_gold`; tables named for what they contain, self-describingly —
  the SQL agent reads these column names and its accuracy depends on them.

## Lake layout

```
s3://fraud-lake-<acct>/
  raw/transactions/dt=YYYY-MM-DD/   # Firehose landing, 30-day lifecycle expiry
  bronze/transactions/              # Iceberg — typed, deduplicated
  silver/transactions/              # Iceberg — enriched, fraud features, SCD merchants
  gold/fraud_metrics_daily/         # Iceberg — aggregates the agent queries
  gold/merchant_risk/
  quarantine/                       # rejected records + rejection_reason
  policies/                         # source PDFs for the knowledge base
  athena-results/                   # query output, 7-day lifecycle expiry
```

## Build order

- **1a** Ingestion + raw landing (producer, S3, Kinesis, Firehose, Athena workgroup)
- **1b** Glue + Iceberg + data quality (bronze/silver/gold jobs, DQDL ruleset, pytest)
- **1c** Orchestration + failure scenarios (Step Functions, EventBridge, alarms, runbook)
- **2** Agent layer (LangGraph supervisor, 3 tools, SQL validator, MCP, FastAPI)
- **3** Packaging (Docker, ECR/Fargate, EKS demo, Lake Formation, CloudTrail, dashboard, CI)
