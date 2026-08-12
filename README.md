# fraud-lake

An AWS streaming lakehouse with an agentic analytics layer, over synthetic card-transaction
fraud data.

```
producer ─▶ Kinesis (on-demand) ─▶ Firehose ─▶ S3 raw/
                                                 │
                                      Glue PySpark (Iceberg)
                                                 │
                              bronze ─▶ silver ─▶ gold      Glue Data Catalog / Athena
                                                 │
                        Step Functions + EventBridge, DQ gate + quarantine
                                                 │
                          LangGraph supervisor on Bedrock ── MCP server ── FastAPI
                          (sql_agent · policy_agent · pipeline_agent)
```

## Status

| Slice | Scope | State |
|---|---|---|
| 1a | Ingestion + raw landing | ✅ code complete, not applied |
| 1b | Glue + Iceberg + data quality | ⬜ not started |
| 1c | Orchestration + failure scenarios | ⬜ not started |
| 2 | Agent layer | ⬜ not started |
| 3 | Packaging (containers, EKS, governance, CI) | ⬜ not started |

## Cost posture

Steady-state target is **under $20–30/month**, and ~$0.35/month between demo sessions once
`make stream-down` has run. Every resource is classified by *pricing shape* in
[COSTS.md](COSTS.md) before it is created. No NAT gateway, no OpenSearch Serverless, no MSK,
no always-on compute in the default state. EKS exists only in a separate workspace that is
created and destroyed the same day.

## Getting started

```bash
make venv          # local virtualenv
make seed-local    # generate synthetic transactions to stdout — no AWS, no cost
make test          # pytest
```

Once AWS credentials are configured (profile `fraud-lake`, region `us-east-1`):

```bash
make init && make plan   # review the plan
make apply               # human-run only
```

## Design decisions

Every locked decision and the alternative it beat is written up in
[docs/decisions.md](docs/decisions.md).
