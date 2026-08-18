# Bedrock Knowledge Base on S3 Vectors, Guardrails, and the agent's IAM role.
#
# COST: everything here is per-request.
#   - S3 Vectors: storage plus a per-query fee. NO idle compute.
#   - Bedrock on-demand: per input/output token. No provisioned throughput, ever.
#   - Guardrails: per text unit evaluated.
#
# The vector store choice is the single most important cost decision in this project.
# OpenSearch Serverless (Classic) has a ~2 OCU minimum — roughly $175/month for a dev
# configuration — billed while completely idle, AND deleting the Knowledge Base does not
# delete the collection it provisioned. It keeps billing from a console you are no longer
# looking at. See docs/decisions.md D-006.
#
# PROVIDER NOTE: the aws_s3vectors_* resources require AWS provider >= 6.0. If your
# provider is older, `terraform init -upgrade` first.

locals {
  policy_prefix = "policies/"
}

# ------------------------------------------------------------------ policy corpus

resource "aws_s3_object" "policy_documents" {
  for_each = fileset("${var.source_root}/policies", "*.md")

  bucket       = var.lake_bucket_id
  key          = "${local.policy_prefix}${each.value}"
  source       = "${var.source_root}/policies/${each.value}"
  etag         = filemd5("${var.source_root}/policies/${each.value}")
  content_type = "text/markdown"
}

# ------------------------------------------------------------------- vector store

resource "aws_s3vectors_vector_bucket" "policies" {
  vector_bucket_name = "${var.name_prefix}-vectors"
}

resource "aws_s3vectors_index" "policies" {
  vector_bucket_name = aws_s3vectors_vector_bucket.policies.vector_bucket_name
  index_name         = "${var.name_prefix}-policy-index"

  data_type = "float32"

  # Must match the embedding model's output dimension exactly. Titan Text Embeddings V2
  # emits 1024 by default; a mismatch here fails at ingestion with an unhelpful error.
  dimension       = var.embedding_dimension
  distance_metric = "cosine"

  metadata_configuration {
    # S3 Vectors caps FILTERABLE metadata at 2048 bytes per vector, and Bedrock stores
    # each chunk's full text in metadata — so every text-bearing key must be declared
    # non-filterable or real documents fail ingestion at the cap. All three keys Bedrock
    # writes are listed because the exact key name has shifted across KB releases;
    # nothing in this project filters on metadata, so nothing is lost.
    non_filterable_metadata_keys = [
      "AMAZON_BEDROCK_TEXT",
      "AMAZON_BEDROCK_TEXT_CHUNK",
      "AMAZON_BEDROCK_METADATA",
    ]
  }
}

# ------------------------------------------------------------------- IAM for the KB

data "aws_iam_policy_document" "kb_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "knowledge_base" {
  name               = "${var.name_prefix}-kb-role"
  assume_role_policy = data.aws_iam_policy_document.kb_assume.json
}

data "aws_iam_policy_document" "knowledge_base" {
  statement {
    sid       = "InvokeEmbeddingModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"]
  }

  statement {
    sid       = "ReadPolicyCorpus"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.lake_bucket_arn}/${local.policy_prefix}*"]
  }

  statement {
    sid       = "ListPolicyPrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.lake_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.policy_prefix}*"]
    }
  }

  statement {
    sid    = "UseVectorIndex"
    effect = "Allow"

    actions = [
      "s3vectors:GetIndex",
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:QueryVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:ListVectors",
    ]

    resources = [
      aws_s3vectors_vector_bucket.policies.vector_bucket_arn,
      aws_s3vectors_index.policies.index_arn,
    ]
  }
}

resource "aws_iam_role_policy" "knowledge_base" {
  name   = "${var.name_prefix}-kb-policy"
  role   = aws_iam_role.knowledge_base.id
  policy = data.aws_iam_policy_document.knowledge_base.json
}

# ----------------------------------------------------------------- knowledge base

resource "aws_bedrockagent_knowledge_base" "policies" {
  name     = "${var.name_prefix}-policies"
  role_arn = aws_iam_role.knowledge_base.arn

  description = "Fraud policy corpus: chargebacks, transaction monitoring, merchant onboarding, data retention."

  knowledge_base_configuration {
    type = "VECTOR"

    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model_id}"
    }
  }

  storage_configuration {
    type = "S3_VECTORS"

    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.policies.index_arn
    }
  }

  depends_on = [aws_iam_role_policy.knowledge_base]
}

resource "aws_bedrockagent_data_source" "policies" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.policies.id
  name              = "${var.name_prefix}-policy-docs"

  data_source_configuration {
    type = "S3"

    s3_configuration {
      bucket_arn         = var.lake_bucket_arn
      inclusion_prefixes = [local.policy_prefix]
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      # Fixed-size chunking with overlap. The policy documents are tabular and
      # section-heavy; a threshold table split across a chunk boundary retrieves as half
      # a table, so the 20% overlap is doing real work here rather than being a default.
      chunking_strategy = "FIXED_SIZE"

      fixed_size_chunking_configuration {
        max_tokens         = var.chunk_max_tokens
        overlap_percentage = 20
      }
    }
  }
}

# --------------------------------------------------------------------- guardrails

resource "aws_bedrock_guardrail" "main" {
  name                      = "${var.name_prefix}-guardrail"
  blocked_input_messaging   = "This request was blocked. Ask about fraud metrics, policy, or pipeline status."
  blocked_outputs_messaging = "The response was withheld because it may contain sensitive information."
  description               = "PII masking on outputs, prompt-injection filtering on inputs."

  content_policy_config {
    # PROMPT_ATTACK is input-only by design — an output filter for it does not exist,
    # and setting an output strength on this filter is rejected by the API.
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }

    dynamic "filters_config" {
      for_each = ["HATE", "INSULTS", "VIOLENCE", "SEXUAL", "MISCONDUCT"]

      content {
        type            = filters_config.value
        input_strength  = "MEDIUM"
        output_strength = "MEDIUM"
      }
    }
  }

  sensitive_information_policy_config {
    # ANONYMIZE, not BLOCK. A fraud analyst legitimately asks questions whose answers
    # brush against identifiers; masking keeps the answer useful while removing the
    # identifier, whereas blocking makes the tool useless for its actual purpose.
    dynamic "pii_entities_config" {
      for_each = [
        "CREDIT_DEBIT_CARD_NUMBER",
        "CREDIT_DEBIT_CARD_CVV",
        "CREDIT_DEBIT_CARD_EXPIRY",
        "US_SOCIAL_SECURITY_NUMBER",
        "US_BANK_ACCOUNT_NUMBER",
        "EMAIL",
        "PHONE",
        "NAME",
        "ADDRESS",
      ]

      content {
        action = "ANONYMIZE"
        type   = pii_entities_config.value
      }
    }

    # Defence in depth: a PAN-shaped string that the managed entity detector misses is
    # still caught structurally.
    regexes_config {
      name        = "pan_like_sequence"
      description = "A 13-19 digit sequence resembling a primary account number"
      pattern     = "\\b(?:\\d[ -]*?){13,19}\\b"
      action      = "ANONYMIZE"
    }
  }

  topic_policy_config {
    topics_config {
      name       = "individual_cardholder_lookup"
      type       = "DENY"
      definition = "Requests to identify, look up, or profile a specific named individual cardholder rather than analyse aggregate fraud patterns."

      examples = [
        "What did John Smith buy last week?",
        "Show me all transactions for the cardholder at 42 Elm Street.",
        "Which customer_id belongs to this phone number?",
      ]
    }
  }
}

resource "aws_bedrock_guardrail_version" "main" {
  guardrail_arn = aws_bedrock_guardrail.main.guardrail_arn
  description   = "Initial published version"
}

# ------------------------------------------------------------------- agent IAM role

data "aws_iam_policy_document" "agent_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }

  # EKS Pod Identity: the demo cluster's pods run as this same role — one identity for
  # the workload wherever it lands. TagSession is how Pod Identity stamps the session
  # with cluster/namespace attribution.
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "agent" {
  name               = "${var.name_prefix}-agent-role"
  assume_role_policy = data.aws_iam_policy_document.agent_assume.json
}

data "aws_iam_policy_document" "agent" {
  # Only the three models the agent actually uses, named explicitly. A wildcard on
  # foundation-model/* would let a config change silently switch to a model costing 20x
  # more per token.
  statement {
    sid       = "InvokeNamedModels"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = var.invocable_model_arns
  }

  statement {
    sid       = "ApplyGuardrail"
    effect    = "Allow"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [aws_bedrock_guardrail.main.guardrail_arn]
  }

  statement {
    sid       = "RetrieveFromKnowledgeBase"
    effect    = "Allow"
    actions   = ["bedrock:Retrieve"]
    resources = [aws_bedrockagent_knowledge_base.policies.arn]
  }

  # Athena: the agent may run and read queries, but only in this workgroup — which is
  # where the 1 GB per-query scan cap is enforced. Without the workgroup restriction the
  # agent could run uncapped queries in the primary workgroup.
  statement {
    sid    = "RunAthenaQueries"
    effect = "Allow"

    actions = [
      "athena:StartQueryExecution",
      "athena:GetQueryExecution",
      "athena:GetQueryResults",
      "athena:StopQueryExecution",
      "athena:GetWorkGroup",
    ]

    resources = ["arn:aws:athena:${var.region}:${var.account_id}:workgroup/${var.athena_workgroup}"]
  }

  # Read-only catalog access, gold only. This is the second line of defence behind the
  # SQL validator's allowlist: even a validator bypass could not read silver.
  statement {
    sid    = "ReadGoldCatalog"
    effect = "Allow"

    actions = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:GetPartitions"]

    resources = [
      "arn:aws:glue:${var.region}:${var.account_id}:catalog",
      "arn:aws:glue:${var.region}:${var.account_id}:database/fraud_gold",
      "arn:aws:glue:${var.region}:${var.account_id}:table/fraud_gold/*",
    ]
  }

  statement {
    sid       = "ReadGoldData"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.lake_bucket_arn, "${var.lake_bucket_arn}/gold/*", "${var.lake_bucket_arn}/warehouse/fraud_gold/*"]
  }

  statement {
    sid       = "ReadModelBundle"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.lake_bucket_arn}/models/*"]
  }

  statement {
    sid       = "ReadOnlineFeatures"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = ["arn:aws:dynamodb:${var.region}:${var.account_id}:table/${var.name_prefix}-customer-features"]
  }

  statement {
    sid       = "WriteAthenaResults"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${var.lake_bucket_arn}/athena-results/*"]
  }

  # pipeline_status is read-only observability.
  statement {
    sid    = "ReadPipelineStatus"
    effect = "Allow"

    actions = [
      "states:ListExecutions",
      "states:DescribeExecution",
      "states:GetExecutionHistory",
      "glue:GetJobRuns",
      "glue:ListJobs",
    ]

    resources = ["*"]
  }

  statement {
    sid       = "PublishAgentMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringLike"
      variable = "cloudwatch:namespace"
      values   = ["fraud-lake/*"]
    }
  }
}

resource "aws_iam_role_policy" "agent" {
  name   = "${var.name_prefix}-agent-policy"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent.json
}
