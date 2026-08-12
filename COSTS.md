# COSTS.md

Running log of every AWS resource this project creates. **A row goes in here *before* the
resource is created, not after.**

Pricing shape legend:
- **Per-request** — costs nothing when idle. Safe.
- **Per-hour, scales to zero** — free when idle *if configured correctly*.
- **Per-hour floor** — bills whether used or not. Needs a teardown step and a calendar reminder.

All dollar figures are estimates for `us-east-1` at a portfolio workload
(~50 events/sec during demo windows, a few GB total storage) and **must be re-verified
against the AWS Pricing Calculator before creating anything** — pricing changes.
The *classification* is the durable part; the numbers are not.

---

## Slice 1a — Ingestion + raw landing

| Resource | Pricing shape | Est. monthly (dev usage) | Teardown step |
|---|---|---|---|
| S3 lake bucket `fraud-lake-<acct>` | Per-request | ~$0.10 (a few GB + requests) | `make destroy` (bucket force-emptied by lifecycle + `force_destroy` in dev) |
| S3 lifecycle: `raw/` expires 30d, `athena-results/` 7d | Per-request | — (reduces cost) | n/a |
| S3 bucket for Terraform remote state | Per-request | <$0.01 | Manual — kept deliberately, it holds state |
| DynamoDB state-lock table (PAY_PER_REQUEST) | Per-request | <$0.01 | Manual — kept with the state bucket |
| Kinesis Data Stream (**on-demand**) | Per-request + small per-stream-hour | ~$0.04/hr stream-hour ≈ **$29/mo if left running** | `make destroy`, or `terraform apply -var enable_stream=false`. **Tear down between demo sessions.** |
| Kinesis Firehose delivery stream | Per-request (per GB ingested) | <$0.10 | `make destroy` |
| Athena workgroup (1 GB per-query scan cap) | Per-request (per TB scanned) | <$0.05 | `make destroy` |
| Glue Data Catalog (databases) | Per-request | $0 (first 1M objects free) | `make destroy` |
| CloudWatch log groups (7-day retention) | Per-request | <$0.10 | `make destroy` |
| IAM roles (Firehose) | Free | $0 | `make destroy` |

**Slice 1a running total: ~$0.35/month if the Kinesis stream is torn down between sessions;
~$29/month if the stream is left up.** The stream is the one thing in this slice with a
meaningful idle cost — see the note below.

### ⚠️ Kinesis on-demand is the cost item to watch in slice 1a
On-demand Kinesis bills a per-stream-hour charge (~$0.036/hr) **plus** per-GB. The hourly
component is small per hour but is ~$26–29/month if the stream sits idle for a full month.
It is *not* a true "per-hour floor" resource in the EKS sense (you can delete it in seconds
and recreate it in seconds), but treat it with the same discipline:

- `enable_stream = false` in `terraform.tfvars` destroys the stream + Firehose and leaves
  the bucket, catalog, and Athena workgroup intact — so the lake stays queryable for free.
- Run `make stream-down` when you finish a demo session.

### Deliberately NOT created in slice 1a (cost decisions)
| Thing | Why skipped | What's used instead |
|---|---|---|
| KMS customer-managed key | ~$1/key/month floor + $0.03/10k requests | SSE-S3 (AES256) by default, free. Flip `use_kms_cmk = true` when you want the CMK screenshot for the security story, then flip it back. |
| NAT Gateway | ~$32/month + per-GB. The classic silent drain. | No VPC needed in slice 1a at all; VPC endpoints in slice 3. |
| Glue crawler on a schedule | Bills per crawler-run | Tables registered directly by the Glue jobs (slice 1b); crawler on demand only if needed. |
| CloudTrail data events | Per-event charge | Deferred to slice 3, scoped to the lake bucket only. |

---

## Slice 1b — Glue + Iceberg + data quality
_(rows go here before the jobs are created)_

| Resource | Pricing shape | Est. monthly | Teardown step |
|---|---|---|---|
| _pending_ | | | |

## Slice 1c — Orchestration
| Resource | Pricing shape | Est. monthly | Teardown step |
|---|---|---|---|
| _pending_ | | | |

## Slice 2 — Agent layer
| Resource | Pricing shape | Est. monthly | Teardown step |
|---|---|---|---|
| _pending_ | | | |

## Slice 3 — Packaging
| Resource | Pricing shape | Est. monthly | Teardown step |
|---|---|---|---|
| _pending_ | | | |

---

## Known per-hour-floor resources in this project

| Resource | Cost while idle | Rule |
|---|---|---|
| **EKS control plane** | ~$73/month, billed the moment the cluster exists, independent of pods | Separate workspace `terraform/envs/eks-demo`. Created and destroyed **the same day**. Set a calendar reminder before `apply`. |
| EKS worker nodes (2 × t3.small) | ~$30/month | Same workspace, same-day teardown. |
| KMS customer-managed key | ~$1/month/key | One key maximum, opt-in via `use_kms_cmk`. |
| Secrets Manager secret | ~$0.40/month/secret | 1–2 secrets maximum. |
| ALB (slice 3) | ~$16/month + LCU | Create only for the Fargate demo window; `desired_count=0` does not stop ALB billing — destroy the ALB too. |

## Never created, at any point
OpenSearch Serverless (Classic) — ~$175–350/month idle, and **deleting a Bedrock Knowledge
Base does not delete the collection it created**; it keeps billing from a different console.
This is the single reason the project uses **S3 Vectors** instead.
MSK / MSK Serverless — per-cluster hourly rate. Bedrock provisioned throughput — hourly commitment.
NAT Gateway. RDS/Aurora provisioned.
