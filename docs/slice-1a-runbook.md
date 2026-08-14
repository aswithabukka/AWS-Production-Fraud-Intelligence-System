# Slice 1a runbook — ingestion + raw landing

Everything below is run by hand — no automation runs `terraform apply` or any
mutating `aws` command unreviewed.

## 0. Prerequisites (once)

```bash
brew install terraform awscli
```

1. Create a **dedicated IAM user** for this project — not your root account — with
   programmatic access.
2. Store its credentials under a profile named `fraud-lake`:
   ```bash
   aws configure --profile fraud-lake     # region us-east-1, output json
   ```
3. **Create an AWS Budget before any resource exists**: $50/month, alerts at
   50% / 80% / 100%. Billing → Budgets → Create budget. This is the backstop for
   everything else in this document.

Verify:

```bash
aws sts get-caller-identity --profile fraud-lake
```

## 1. Remote state (once)

```bash
cd terraform/bootstrap
terraform init
terraform plan
terraform apply
```

Then copy the two outputs into the commented backend block in
`terraform/envs/dev/versions.tf`, uncomment it, and run:

```bash
cd ../envs/dev && terraform init -migrate-state
```

Skipping this is survivable — local state works — but remote state is the thing a
reviewer expects to see, and it costs cents per year.

## 2. Plan the dev stack

```bash
cp terraform/envs/dev/terraform.tfvars.example terraform/envs/dev/terraform.tfvars
make init
make plan
```

Read the plan. Expect roughly 25 resources: one S3 bucket and its configuration
resources, four Glue databases, one Athena workgroup, one raw external table, two named
queries, one Kinesis stream, one Firehose delivery stream, one IAM role and policy, one
log group.

**The only line item with an idle cost is the Kinesis stream.** If you see a NAT gateway,
a KMS key you didn't ask for, or anything with "cluster" in the name, stop.

```bash
make apply
```

## 3. Produce data

```bash
make seed        # 60s at ~50 events/sec against the live stream
```

or directly, for more control:

```bash
python -m ingestion.producer \
  --stream $(terraform -chdir=terraform/envs/dev output -raw kinesis_stream_name) \
  --profile fraud-lake --duration 120 --rate 50
```

Firehose buffers for 300 seconds by default, so **wait up to 5 minutes** before expecting
objects in S3. Set `firehose_buffer_interval_seconds = 60` in `terraform.tfvars` if you
want faster feedback while developing.

## 4. Verify the landing zone

```bash
aws s3 ls s3://$(terraform -chdir=terraform/envs/dev output -raw lake_bucket)/raw/transactions/ \
  --recursive --profile fraud-lake --human-readable
```

Then in Athena — select workgroup `fraud-lake`, database `fraud_raw`:

```sql
SELECT dt, count(*) AS records
FROM fraud_raw.transactions
GROUP BY dt
ORDER BY dt DESC;
```

Both smoke queries are saved in the workgroup as `1a-raw-row-count` and
`1a-raw-fraud-mix`. The fraud share should land near 1.5% modulated upward by velocity
bursts, and `ecommerce` should be over-represented among fraudulent rows.

## 5. Capture for the README

1. S3 console showing `raw/transactions/dt=YYYY-MM-DD/` with GZIP objects.
2. Athena result of `1a-raw-fraud-mix`, with the **data scanned** figure visible — that
   number is what the 1 GB workgroup cap is protecting.
3. Firehose monitoring tab: incoming records, delivery to S3 success.
4. The producer's closing JSON summary (records emitted, throughput, fraud share).

## 6. Shut down

**Every time you finish a session:**

```bash
make stream-down     # destroys Kinesis + Firehose, keeps the lake queryable
```

Away for more than a few days, or done with the slice:

```bash
make destroy         # tears down the whole dev stack
```

`terraform/bootstrap` is intentionally not destroyed — it holds the state file.

## Cost check

```bash
make cost            # month-to-date, grouped by service, filtered to Project=fraud-lake
```

Do this every Monday. Slice 1a should read well under a dollar with the stream torn
down between sessions.
