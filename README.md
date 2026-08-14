# fraud-lake

An AWS streaming lakehouse with an agentic analytics layer, over synthetic
card-transaction fraud data. Built as a portfolio project: every design decision is
recorded in [docs/decisions.md](docs/decisions.md), and every resource is classified by
pricing shape in [COSTS.md](COSTS.md) *before* it is created.

```
producer ─▶ Kinesis (on-demand) ─▶ Firehose ─▶ S3 raw/            [slice 1a]
                                                 │
                                    Glue 5.x PySpark (Iceberg)     [slice 1b]
                                                 │
                          bronze ──▶ silver ──▶ gold               Glue Catalog / Athena
                             │          │
                         quarantine   DQDL quality gates
                                                 │
                EventBridge ─▶ Step Functions ─▶ SNS alarms        [slice 1c]
                                                 │
              LangGraph supervisor on Bedrock (Converse API)       [slice 2]
              route_query ─▶ call_tool ─▶ synthesize  (MAX_ITERATIONS=5)
                 │                │
                 │        ┌───────┼──────────────┐
                 │   query_lakehouse  search_policies  pipeline_status
                 │   (SQL validator)  (KB on S3 Vectors)  (SFN + Glue history)
                 │
              MCP server ── FastAPI ── Docker ── ECR ── Fargate    [slice 3]
              Lake Formation personas · CloudTrail · dashboard · EKS (same-day demo)
```

## Status

| Slice | Scope | State |
|---|---|---|
| 1a | Ingestion + raw landing | ✅ **deployed & verified** |
| 1b | Glue + Iceberg + data quality | ✅ **deployed & verified** |
| 1c | Orchestration + all 3 failure scenarios | ✅ **deployed & executed** |
| 2 | Agent layer (LangGraph, MCP, FastAPI) | ✅ **deployed — definition-of-done met** |
| 3 | Governance (Lake Formation personas, CloudTrail) | ✅ **deployed & demoed** |
| 3 | Containers (ECR/Fargate) · EKS demo · GitHub CI | ⬜ code complete, awaiting Docker install / demo window |

**Everything above ran against a live AWS account.** The measured results — including
the seven diagnosed failures before the first green run, and the numbers for every
failure scenario — are in [docs/as-run-results.md](docs/as-run-results.md).

## Try it now, with no AWS account

```bash
make venv
make test          # 142 tests: transforms, SQL validator attacks, supervisor loop, API
make seed-local    # synthetic transactions to stdout
make api-local     # FastAPI on :8000 — /docs, /health, /sql/check work offline
```

The SQL validator demo needs nothing but the local API:

```bash
curl -s localhost:8000/sql/check -X POST -H 'content-type: application/json' \
  -d '{"sql": "SELECT dt FROM fraud_gold.fraud_metrics_daily; DROP TABLE fraud_gold.fraud_metrics_daily"}'
# -> {"status":"rejected","reason":"expected exactly 1 statement, found 2 — stacked statements are rejected"}
```

## Cost posture

Steady state: **~$0.35/month** with the stream down, **under ~$30/month** with everything
except EKS running. The rules that keep it there:

- Every flag in `terraform.tfvars` defaults to the cheap option; things that bill while
  idle (Kinesis stream, ALB, VPC endpoints, EKS) are opt-in and called out in-line.
- `make stream-down` removes the only slice-1 resource with an idle cost; the lake stays
  queryable.
- Athena workgroup enforces a 1 GB per-query scan cap. Glue capped at 2×G.1X/15 min.
  Bedrock on-demand only, two model tiers, `max_tokens` capped, `MAX_ITERATIONS=5`.
- No NAT gateway anywhere — and the network module documents why blindly replacing a NAT
  with 10 interface endpoints would *quadruple* the cost at this scale.
- EKS lives in its own workspace with its own state, created and destroyed the same day.

## Layout

```
ingestion/       producer + generator (3 injectable fraud archetypes)
glue/            PySpark transforms (pure functions) + job entry points
quality/         DQDL rulesets + quality gate job
orchestration/   Step Functions ASL + 4 Lambdas
agents/          SQL validator, Bedrock client, catalog cache, LangGraph supervisor
mcp_server/      MCP server over the same tools
api/             FastAPI service (ask, ask/stream, sql/check, health, metrics)
k8s/             namespace/deployment/service/HPA (+ opt-in ingress) via kustomize
terraform/       bootstrap (state) · envs/dev · envs/eks-demo · 9 modules
policies/        synthetic fraud-policy corpus for the knowledge base
tests/           142 tests, all runnable offline
docs/            decisions, runbooks, failure scenarios
```

## The three failure scenarios

Reproducible on demand, not waited for — see [docs/failure-scenarios.md](docs/failure-scenarios.md):

1. **Schema evolution** — `--schema-version 2` adds a field; Iceberg evolves additively;
   time-travel queries against pre-evolution snapshots still work.
2. **Corrupted batch** — `--corrupt-rate 0.35`; records quarantined with reasons, the DQ
   gate fails, downstream is *skipped* (gold goes stale, not wrong), the alarm fires.
3. **Duplicate replay** — `--replay` the same batch; dedupe + Iceberg MERGE hold row
   counts flat, proven by snapshot history.
```
