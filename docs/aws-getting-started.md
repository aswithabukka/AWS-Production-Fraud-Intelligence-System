# AWS from zero — a guided walkthrough for this project

This is the order to do things, what each step actually does, and where to look in the
console so you understand the backend rather than just clicking through it. Budget ~2–3
hours for Phases 0–5 the first time.

Rule of thumb throughout: **the console is for looking, Terraform is for changing.**
Anything you create by hand in the console is invisible to `make destroy`, and that is how
orphaned resources end up billing you.

---

## Phase 0 — Protect the account (15 min, do this before anything else)

You will be using your **root account** (the email you signed up with) exactly twice:
to secure it, and to create your working IAM user. After that, never again.

1. Sign in at https://console.aws.amazon.com as root.
2. **Turn on MFA for root.** Top-right menu (your account name) → *Security credentials*
   → *Multi-factor authentication* → *Assign MFA device*. Use any authenticator app.
   Why: root can do anything, including delete billing alarms. It is the one identity
   that must never be phished.
3. **Set the budget — before any resource exists.**
   Search bar (top of console) → type **Billing** → *Budgets* → *Create budget*:
   - Template: **Monthly cost budget**
   - Amount: **$50**
   - Email recipients: your email
   - Then edit it and add alert thresholds at **50%, 80%, 100%**.
   Why first: every other cost control in this project can fail silently. This one emails
   you. It is the backstop for all of them.
4. While in Billing, open *Billing preferences* → enable **"Receive Free Tier usage
   alerts"** — a second free early-warning channel.

## Phase 1 — Create your working identity (15 min)

Still as root, last time:

1. Search **IAM** → *Users* → *Create user*.
   - Name: `fraud-lake-admin`
   - "Provide user access to the Management Console" → **yes** (you'll want console
     access for looking around), set a password.
2. Permissions: *Attach policies directly* → **AdministratorAccess**.
   - Yes, the project bans `AdministratorAccess` *in the code it writes* — every role
     Terraform creates is narrowly scoped. But the human running Terraform needs broad
     access to create those roles. That distinction (broad human operator, narrow machine
     roles) is itself an interview talking point.
3. Create the user, then open it → *Security credentials* tab → **Create access key**
   → use case "Command Line Interface (CLI)". Copy both values — the secret is shown once.
4. Enable MFA for this user too.
5. **Sign out of root.** From now on you sign in as `fraud-lake-admin` at the account's
   IAM sign-in URL (shown on the IAM dashboard).

## Phase 2 — Wire up your laptop (10 min)

```bash
brew install terraform awscli
aws configure --profile fraud-lake
```

Enter the access key id, the secret, region `us-east-1`, output `json`. This writes
`~/.aws/credentials` — which is exactly why the repo never contains a key: boto3 and
Terraform both read that file via the profile name.

Verify the identity chain works end to end:

```bash
aws sts get-caller-identity --profile fraud-lake
```

You should see your account id and `user/fraud-lake-admin`. That command is your
debugging friend forever: it answers "who does AWS think I am right now?"

## Phase 3 — Remote state (10 min, once)

**What this is for:** Terraform tracks what it created in a "state file". Kept only on
your laptop, it is one disk failure away from Terraform forgetting your infrastructure
exists (the resources keep billing; Terraform just can't see them). So state goes in S3,
with a DynamoDB table as a lock so two applies can't corrupt it.

```bash
cd ~/Job/fraud-lake/terraform/bootstrap
terraform init      # downloads the AWS provider plugin
terraform plan      # shows WHAT WOULD happen — read it, nothing is created yet
terraform apply     # type 'yes' — creates the state bucket + lock table
```

Copy the two outputs into the commented `backend "s3"` block in
`terraform/envs/dev/versions.tf`, uncomment it, then:

```bash
cd ../envs/dev && terraform init -migrate-state
```

**Console tour:** S3 → you'll see `fraud-lake-tfstate-<account>`. DynamoDB → *Tables* →
`fraud-lake-tflock`. That's all remote state is — a JSON file in a bucket and a lock row.

## Phase 4 — Slice 1a: the lake and the stream (30 min)

```bash
cd ~/Job/fraud-lake
cp terraform/envs/dev/terraform.tfvars.example terraform/envs/dev/terraform.tfvars
make plan
```

**Read the plan.** This is the habit that keeps the bill at zero. You're checking:
- resource count roughly matches expectation (~25–30 for 1a with the default flags)
- nothing named `nat`, `cluster`, or `kms_key` (unless you opted in)
- the only per-hour item is the Kinesis stream

```bash
make apply     # it asks you to type 'apply' — that's the cost-control speed bump
```

First applies fail sometimes on a provider-schema nit — paste the error to Claude Code
and re-plan. That's normal, not a sign something is broken.

### Console tour — walk the data path you just built

1. **S3** → `fraud-lake-<account>` → see the zone prefixes (`raw/`, `bronze/`, …,
   `quarantine/`). One bucket, prefixes as zones. *Properties* tab → default encryption
   (SSE-S3) and the lifecycle rules.
2. **Kinesis** → *Data streams* → `fraud-lake-transactions`. On-demand mode — no shard
   math. The *Monitoring* tab is where incoming records appear.
3. **Amazon Data Firehose** → `fraud-lake-raw`. Look at the destination settings: the
   `dt=!{timestamp:yyyy-MM-dd}/` prefix is what creates date partitioning, and the
   300-second buffer is why data takes up to 5 minutes to appear in S3.
4. **Athena** → switch workgroup (top right) to `fraud-lake`. *Settings* shows the 1 GB
   per-query cap. Saved queries → the two `1a-*` smoke tests.
5. **IAM** → *Roles* → `fraud-lake-firehose-role` → *Permissions*. Read the policy: it
   can read one stream and write one S3 prefix. This narrowness is the pattern every
   role in the project follows.

### Send data through it

```bash
make seed        # 60s of synthetic transactions into Kinesis
```

Watch it flow: Kinesis *Monitoring* (records in) → wait ~5 min → S3 `raw/transactions/dt=…/`
(objects land) → Athena, run `1a-raw-row-count` (query the data where it lies — nothing
was loaded anywhere; that's the point of a lake).

Then end the session:

```bash
make stream-down   # removes the only idle-cost resource; the lake stays queryable
```

## Phase 5 — Slices 1b + 1c: transforms and orchestration (45 min)

These are already wired in the same `envs/dev` stack, so:

```bash
make plan && make apply    # adds Glue jobs, Step Functions, Lambdas, alarms
make stream-up && make seed
make run-pipeline          # or just wait — S3 arrival triggers it via Lambda
```

### Console tour — watch a pipeline run

1. **Step Functions** → `fraud-lake-pipeline` → open the running execution. The **graph
   view** is the mental model of the whole backend: bronze → gate → silver → gate → gold,
   with the failure branch drawn. Click any state to see its input/output JSON.
2. **AWS Glue** → *ETL jobs* → click a job → *Runs*. Each run shows DPU-hours — that
   number × the DPU price is what the run cost (cents).
3. **CloudWatch** → *Log groups* → `/aws-glue/jobs/fraud-lake` — the Spark logs, including
   the bronze job's quarantine breakdown.
4. **Athena**: `SELECT * FROM fraud_gold.fraud_metrics_daily LIMIT 20` — the tables the
   agent will query.
5. **CloudWatch** → *Dashboards* → `fraud-lake` — pipeline and agent on one pane.

Then run the three failure exercises in [failure-scenarios.md](failure-scenarios.md) —
schema evolution, corrupted batch, duplicate replay. Screenshot each; they're the README's
best material, and reproducing failure on demand is what "understanding the backend"
actually means.

## Phase 6 — Slice 2: the agent layer (30 min)

One manual console step first — **Bedrock model access**:
Bedrock console → *Model access* (bottom of left nav) → *Modify model access* → enable
**Claude Haiku 4.5**, **Claude Sonnet 4.5**, **Titan Text Embeddings V2**. Approval is
usually instant. Terraform cannot do this step; it's an account-level agreement.

```bash
# in terraform.tfvars:  enable_agent_layer = true
terraform -chdir=terraform/envs/dev init -upgrade   # S3 Vectors needs provider 6.x
make plan && make apply
```

Then run the API locally against the real backend:

```bash
export KNOWLEDGE_BASE_ID=$(terraform -chdir=terraform/envs/dev output -raw knowledge_base_id)
export GUARDRAIL_ID=$(terraform -chdir=terraform/envs/dev output -raw guardrail_id)
export STATE_MACHINE_ARN=$(terraform -chdir=terraform/envs/dev output -raw state_machine_arn)
export ATHENA_OUTPUT_LOCATION=s3://$(terraform -chdir=terraform/envs/dev output -raw lake_bucket)/athena-results/
AWS_PROFILE=fraud-lake make api-local
```

The definition-of-done questions:

```bash
curl -s localhost:8000/ask -X POST -H 'content-type: application/json' \
  -d '{"question":"Compare fraud rate by MCC for the last 30 days versus the prior 30."}' | python3 -m json.tool

curl -s localhost:8000/ask -X POST -H 'content-type: application/json' \
  -d '{"question":"What does policy say about chargeback thresholds?"}' | python3 -m json.tool

curl -s localhost:8000/ask -X POST -H 'content-type: application/json' \
  -d '{"question":"Did last night'\''s pipeline run succeed?"}' | python3 -m json.tool
```

Every answer comes back with the SQL that ran or the citations it used — check them.

**Console tour:** Bedrock → *Knowledge bases* → see the S3 Vectors data source and sync
status. CloudWatch → dashboard → the agent row now shows tokens, latency, tool calls.

## Phase 7 — Slice 3, only when you want the screenshots

Each of these has a cost while it exists; turn on, capture, turn off:

| Want | Flip in tfvars | Cost while on |
|---|---|---|
| Images in ECR + ECS service | `enable_containers = true` | $0 at rest |
| A running Fargate task | `make demo-up` … `make demo-down` | ~1¢/hr |
| Public URL | `enable_alb = true` + your IP in `allowed_ingress_cidrs` | ~$16/mo |
| Personas demo | `enable_lake_formation = true` | free |
| Audit trail | `enable_cloudtrail = true` | pennies |
| EKS | separate: see `terraform/envs/eks-demo/README.md` | **~$0.14/hr — same-day teardown** |

## The weekly habit

```bash
make cost    # month-to-date, by service, filtered to Project=fraud-lake
```

Or console: **Cost Explorer** → group by *Service* → filter tag `Project = fraud-lake`.
Every Monday. If the number surprises you, something has an hourly floor you didn't
classify — check EKS, ALB, endpoints, and the Kinesis stream first.

## When you're done for a while

```bash
make stream-down          # between sessions
make destroy              # leaving for weeks — lake included
make eks-destroy          # if you EVER applied eks-demo
```

The bootstrap (state bucket + lock table) is deliberately kept — it costs cents/year.
