# As-run results — what actually happened

Everything below was measured on the live deployment in account `…9277`, us-east-1,
2026-08-13 → 2026-08-15. Numbers are real, not projected.

## The road to the first green run

Seven diagnosed failures preceded the first end-to-end success, each caught, reported to
S3, and emailed by the pipeline's own failure machinery — which therefore got seven live
tests before its first success:

| Run | Failed at | Root cause (all fixed in commit history) |
|---|---|---|
| 1–2 | bronze job | Iceberg session extensions are static Spark configs — must be set before the context exists |
| 3 | bronze gate | Glue DQ publishes to the AWS-owned `Glue Data Quality` metric namespace; the role's namespace condition denied it |
| 4 | bronze gate | `EvaluateDataQuality.process_rows` returns a DynamicFrameCollection, not a frame |
| 5 | bronze gate | Hand-rolled DQDL freshness (`ColumnValues > now() - 3 hours`) evaluated to 0% on fresh data |
| 6 | silver gate | Boolean column compared to string literals poisoned the whole evaluation; plus DQDL `between` is boundary-exclusive |
| 7 | gold job | `F.nullif` unsupported by Glue 5's Spark despite working in stock PySpark 3.5 |
| **8** | — | **PipelineSucceeded** (11.8 min end to end) |

## Failure scenario A — Iceberg schema evolution ✅

Producer re-run with `--schema-version 2` (adds `auth_response_code`). No pipeline code
changed.

- **16,067 v1 rows** (column NULL) coexist with **2,935 v2 rows** (column populated)
- The evolution is one metadata commit in the snapshot log — no rewrite
- Time travel: `FOR VERSION AS OF 6668896179065563007` (pre-evolution) returns exactly
  16,067 rows — history queryable as it was

## Failure scenario B — corrupted batch ✅ (with a lesson)

Producer re-run with `--corrupt-rate 0.35`: 995 of 2,921 records malformed.

- Bronze job **succeeded** (isolating bad data is its job); malformed records landed in
  `quarantine/` with per-rule rejection reasons
- 146 unknown-merchant records passed structural checks, entered bronze, and tripped the
  **referential anti-join**: gate verdict 13/14 → Choice state took the failure branch →
  silver/gold skipped → SNS alert
- Gold stayed **stale, not wrong**: its compute stamp predated the poison
- **The lesson (first remediation failed):** deleting orphans from bronze alone didn't
  stick — bronze re-derives from raw each run and faithfully re-imported them. Real
  remediation = remove the poison objects from raw **and** `DELETE FROM` bronze
  (Iceberg row-level delete), then re-run → green, 14/14.

## Failure scenario C — duplicate replay ✅

The identical 3,000-event file delivered and fully processed twice.

| | total rows | distinct ids |
|---|---|---|
| before duplicate | 23,926 | 23,926 |
| after duplicate | **23,926** | **23,926** |

Iceberg's snapshot log shows a *new commit* with identical `total-records`: the MERGE ran
and changed nothing. Two mechanisms enforce this — in-batch dedupe (latest by ingest
time) and `MERGE INTO` on `transaction_id`.

## Agent layer — definition of done ✅

All three question types answered by the deployed agent (local API → Bedrock → tools):

- **Data**: fraud-rate-by-day comparison — generated SQL passed the validator, ran on
  Athena, and the figures matched a hand-written control query to the cent
  ($578,569.22 / $204,310.86). ~15 s, ~1,450 tokens (≈ a fifth of a cent)
- **Policy**: chargeback thresholds answered with the full four-tier table and `[1]`
  citations to `chargeback-and-dispute-policy.md` (retrieval scores 0.83/0.75/0.66)
- **Ops**: correctly identified the last successful execution, its 11.8-min duration,
  per-stage timings, and the preceding failures

S3 Vectors ingestion gotcha: every `AMAZON_BEDROCK_TEXT*` metadata key must be declared
non-filterable — filterable metadata caps at 2,048 bytes/vector and chunk text lives in
metadata. First ingestion failed 3/4 documents until fixed; after: 3/3.

## Governance — column-level control ✅

The same `SELECT * FROM fraud_gold.fraud_metrics_daily` as two personas:

| persona | columns returned |
|---|---|
| `fraud-lake-analyst` | **14** — `distinct_customer_count`, `total_fraud_signals` absent |
| `fraud-lake-risk-analyst` | **16** — full table |

Enforced by Lake Formation at the catalog; the exclusion survives `SELECT *`.
Three lessons that only live deployment teaches:

1. IAM `AdministratorAccess` cannot manage LF grants — the caller must be registered as
   a *data lake administrator* first
2. The account-default `IAMAllowedPrincipals ALL` grant shadows column filters until
   explicitly revoked per table — and the pipeline/agent roles need their own explicit
   grants **before** that revocation
3. Data lake administrators hold *grant* power, not implicit *data* access — revoking
   the default cut off the admin identity itself until it received an explicit SELECT

CloudTrail: one management trail + S3 data events scoped to the lake bucket only.

## Cost, actual

Everything above — ~29,000 events streamed, 15+ pipeline runs, two days of iteration,
the agent layer, all three scenarios — ran on **roughly $2–3 of gross usage**, absorbed
by credits. Steady state with the stream down remains ~$0.35/month.

## Still open (optional)

- Docker → ECR → Fargate deploy (blocked on installing Docker Desktop locally)
- EKS same-day demo (`terraform/envs/eks-demo`, ~$0.14/hr while up)
- Push the repo to GitHub (no remote configured yet) to light up the Actions pipeline
