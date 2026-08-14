data "aws_caller_identity" "current" {}

data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # Repo root, so modules can upload job scripts and the policy corpus without every
  # module needing its own relative-path arithmetic.
  source_root = abspath("${path.module}/../../..")

  gold_tables = ["fraud_gold.fraud_metrics_daily", "fraud_gold.merchant_risk"]

  model_arns = [
    for id in toset([var.routing_model_id, var.sql_model_id, var.synthesis_model_id]) :
    # Inference-profile ids (the "us." prefix) are regional resources; plain model ids are
    # partition-level foundation models with no account in the ARN.
    startswith(id, "us.") || startswith(id, "eu.") || startswith(id, "apac.")
    ? "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/${id}"
    : "arn:aws:bedrock:${var.region}::foundation-model/${id}"
  ]

  # Cross-region inference profiles route to any region in the geography, so the
  # underlying foundation models must be invocable too.
  foundation_model_arns = [
    for id in toset([var.routing_model_id, var.sql_model_id, var.synthesis_model_id]) :
    "arn:aws:bedrock:*::foundation-model/${replace(id, "/^(us|eu|apac)\\./", "")}"
  ]
}

# ================================================================= SLICE 1a: storage
# Per-request pricing. Safe to leave up — this is what keeps the lake queryable after
# `make stream-down`.

module "lake" {
  source = "../../modules/s3_lake"

  name_prefix                   = var.name_prefix
  account_id                    = local.account_id
  use_kms_cmk                   = var.use_kms_cmk
  force_destroy                 = var.force_destroy_buckets
  raw_retention_days            = var.raw_retention_days
  athena_results_retention_days = var.athena_results_retention_days
}

module "athena" {
  source = "../../modules/athena"

  workgroup_name       = var.name_prefix
  lake_bucket_id       = module.lake.bucket_id
  kms_key_arn          = module.lake.kms_key_arn
  bytes_scanned_cutoff = var.athena_scan_cutoff_bytes
  force_destroy        = var.force_destroy_buckets
}

# ---------------------------------------------------------------------------------
# COST GATE. The Kinesis stream bills per stream-hour whether or not anything is written
# to it (~$26-29/month if left up). `make stream-down` removes the ingest path and leaves
# the lake, catalog, and workgroup intact and queryable.

module "ingest" {
  source = "../../modules/kinesis_ingest"
  count  = var.enable_stream ? 1 : 0

  name_prefix             = var.name_prefix
  account_id              = local.account_id
  lake_bucket_arn         = module.lake.bucket_arn
  kms_key_arn             = module.lake.kms_key_arn
  buffer_size_mb          = var.firehose_buffer_size_mb
  buffer_interval_seconds = var.firehose_buffer_interval_seconds
  log_retention_days      = var.log_retention_days
}

# ============================================================ SLICE 1b: Glue + Iceberg
# Per-request. A Glue job that never runs bills nothing, so these are safe to define.
# Requires `make package` first — the module uploads the library archive it builds.

module "glue" {
  source = "../../modules/glue_jobs"

  name_prefix   = var.name_prefix
  region        = var.region
  account_id    = local.account_id
  source_root   = local.source_root
  libs_zip_path = "${local.source_root}/dist/glue_libs.zip"
  libs_zip_hash = filemd5("${local.source_root}/dist/glue_libs.zip")

  lake_bucket_id  = module.lake.bucket_id
  lake_bucket_arn = module.lake.bucket_arn
  kms_key_arn     = module.lake.kms_key_arn
  databases       = module.athena.database_names

  worker_type         = var.glue_worker_type
  number_of_workers   = var.glue_number_of_workers
  job_timeout_minutes = var.glue_timeout_minutes
  log_retention_days  = var.log_retention_days
}

# ========================================================== SLICE 1c: orchestration
# Step Functions is per-state-transition, Lambda is inside the free tier at this volume,
# and the hourly schedule is DISABLED by default — an armed schedule runs Glue every hour
# over zero new rows.

module "orchestration" {
  source = "../../modules/orchestration"

  name_prefix     = var.name_prefix
  region          = var.region
  account_id      = local.account_id
  source_root     = local.source_root
  lake_bucket_id  = module.lake.bucket_id
  lake_bucket_arn = module.lake.bucket_arn
  glue_job_names  = module.glue.job_names

  alert_email        = var.alert_email
  enable_schedule    = var.enable_schedule
  enable_s3_trigger  = var.enable_s3_trigger
  freshness_hours    = var.freshness_hours
  log_retention_days = var.log_retention_days

  knowledge_base_id             = var.enable_agent_layer ? module.bedrock[0].knowledge_base_id : ""
  knowledge_base_data_source_id = var.enable_agent_layer ? module.bedrock[0].data_source_id : ""
}

# ============================================================== SLICE 2: agent layer
# Bedrock on-demand, S3 Vectors, and Guardrails — all per-request, no idle cost.
# Gated only because it needs Bedrock model access enabled in the console first.

module "bedrock" {
  source = "../../modules/bedrock"
  count  = var.enable_agent_layer ? 1 : 0

  name_prefix     = var.name_prefix
  region          = var.region
  account_id      = local.account_id
  source_root     = local.source_root
  lake_bucket_id  = module.lake.bucket_id
  lake_bucket_arn = module.lake.bucket_arn

  athena_workgroup     = module.athena.workgroup_name
  invocable_model_arns = concat(local.model_arns, local.foundation_model_arns)
}

# ================================================================ SLICE 3: packaging

module "network" {
  source = "../../modules/network"
  count  = var.enable_containers ? 1 : 0

  name_prefix     = var.name_prefix
  region          = var.region
  networking_mode = var.networking_mode
}

module "ecs" {
  source = "../../modules/ecs"
  count  = var.enable_containers ? 1 : 0

  name_prefix = var.name_prefix
  region      = var.region

  vpc_id            = module.network[0].vpc_id
  public_subnet_ids = module.network[0].public_subnet_ids
  task_subnet_ids   = module.network[0].task_subnet_ids
  assign_public_ip  = module.network[0].tasks_need_public_ip

  task_role_arn = var.enable_agent_layer ? module.bedrock[0].agent_role_arn : var.fallback_task_role_arn
  api_image     = var.api_image

  desired_count         = var.ecs_desired_count
  enable_alb            = var.enable_alb
  allowed_ingress_cidrs = var.allowed_ingress_cidrs
  log_retention_days    = var.log_retention_days

  environment = {
    AWS_REGION             = var.region
    ATHENA_WORKGROUP       = module.athena.workgroup_name
    ATHENA_DATABASE        = "fraud_gold"
    ATHENA_OUTPUT_LOCATION = module.athena.results_uri
    STATE_MACHINE_ARN      = module.orchestration.state_machine_arn
    KNOWLEDGE_BASE_ID      = var.enable_agent_layer ? module.bedrock[0].knowledge_base_id : ""
    GUARDRAIL_ID           = var.enable_agent_layer ? module.bedrock[0].guardrail_id : ""
    GUARDRAIL_VERSION      = var.enable_agent_layer ? module.bedrock[0].guardrail_version : "DRAFT"
    ROUTING_MODEL_ID       = var.routing_model_id
    SQL_MODEL_ID           = var.sql_model_id
    SYNTHESIS_MODEL_ID     = var.synthesis_model_id
    MAX_ITERATIONS         = tostring(var.max_iterations)
  }
}

module "governance" {
  source = "../../modules/governance"

  # The identity running terraform becomes the LF data lake administrator —
  # session context resolves an assumed-role session back to the role ARN.
  lake_formation_admin_arn = data.aws_iam_session_context.current.issuer_arn
  glue_role_arn            = module.glue.role_arn
  agent_role_arn           = try(module.bedrock[0].agent_role_arn, "")

  name_prefix     = var.name_prefix
  region          = var.region
  account_id      = local.account_id
  lake_bucket_arn = module.lake.bucket_arn

  athena_workgroup      = module.athena.workgroup_name
  enable_lake_formation = var.enable_lake_formation
  enable_cloudtrail     = var.enable_cloudtrail
  force_destroy_buckets = var.force_destroy_buckets
}

module "observability" {
  source = "../../modules/observability"

  name_prefix       = var.name_prefix
  region            = var.region
  state_machine_arn = module.orchestration.state_machine_arn
  glue_job_names    = values(module.glue.job_names)
  glue_log_group    = module.glue.log_group_name
  max_iterations    = var.max_iterations
}
