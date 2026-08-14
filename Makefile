SHELL := /bin/bash
.DEFAULT_GOAL := help

AWS_PROFILE ?= fraud-lake
AWS_REGION  ?= us-east-1
TF_DIR      ?= terraform/envs/dev
TF          := terraform -chdir=$(TF_DIR)

export AWS_PROFILE
export AWS_REGION

# ---------------------------------------------------------------- help

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- local dev

venv: ## Create the local virtualenv and install all dev deps
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

lint: ## ruff + terraform fmt check
	./.venv/bin/ruff check .
	./.venv/bin/ruff format --check .
	terraform fmt -recursive -check terraform/ 2>/dev/null || true

fmt: ## Auto-format python + terraform
	./.venv/bin/ruff format .
	./.venv/bin/ruff check --fix .
	terraform fmt -recursive terraform/ 2>/dev/null || true

test: ## Run the full pytest suite (no AWS calls, no cost)
	./.venv/bin/pytest -q

api-local: ## Run the FastAPI service locally (uvicorn on :8000)
	./.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000

mcp-local: ## Run the MCP server locally (stdio)
	./.venv/bin/python -m mcp_server.server

package: ## Build dist/glue_libs.zip (required before terraform plan)
	rm -rf dist && mkdir -p dist
	cd . && zip -qr dist/glue_libs.zip glue quality \
		-x '*__pycache__*' -x '*.pyc'
	@echo "built dist/glue_libs.zip ($$(du -h dist/glue_libs.zip | cut -f1))"

# ---------------------------------------------------------------- terraform

init: ## terraform init (backend values from backend.hcl)
	$(TF) init -backend-config=backend.hcl

plan: package ## terraform plan (ALWAYS run before apply)
	$(TF) plan -out=tfplan

apply: ## terraform apply (human-run only — review the plan first)
	@echo "Resources with an idle cost in this stack, per your tfvars:"
	@echo "  - Kinesis stream (~\$$26-29/mo while up)  -> make stream-down when idle"
	@echo "  - ALB, if enable_alb=true (~\$$16/mo)     -> billed regardless of task count"
	@echo "  - VPC endpoints, if networking_mode=endpoints (~\$$130/mo)"
	@read -p "Type 'apply' to continue: " ans; [ "$$ans" = "apply" ] || exit 1
	$(TF) apply tfplan

destroy: ## Tear down EVERYTHING in the dev env
	@read -p "This destroys the whole dev stack. Type 'destroy' to continue: " ans; [ "$$ans" = "destroy" ] || exit 1
	$(TF) destroy

output: ## Show terraform outputs
	$(TF) output

# ---------------------------------------------------------------- cost levers

stream-down: ## Destroy just Kinesis + Firehose; the lake stays queryable
	$(TF) apply -var 'enable_stream=false'

stream-up: ## Recreate Kinesis + Firehose for a demo session
	$(TF) apply -var 'enable_stream=true'

demo-up: ## Bring up one Fargate task for a demo (requires enable_containers)
	$(TF) apply -var 'ecs_desired_count=1'

demo-down: ## Scale the Fargate service back to zero
	$(TF) apply -var 'ecs_desired_count=0'

cost: ## Month-to-date spend for this project, grouped by service
	@aws ce get-cost-and-usage \
		--time-period Start=$$(date -v1d +%Y-%m-%d),End=$$(date -v+1d +%Y-%m-%d) \
		--granularity MONTHLY --metrics UnblendedCost \
		--group-by Type=DIMENSION,Key=SERVICE \
		--filter '{"Tags":{"Key":"Project","Values":["fraud-lake"]}}' \
		--profile $(AWS_PROFILE) --output table

eks-destroy: ## Tear down the EKS demo workspace — RUN THE SAME DAY YOU APPLIED IT
	terraform -chdir=terraform/envs/eks-demo destroy

# ---------------------------------------------------------------- data

seed: ## Run the producer against the live Kinesis stream (60s, ~50 eps)
	./.venv/bin/python -m ingestion.producer --duration 60 --rate 50 \
		--stream $$($(TF) output -raw kinesis_stream_name)

seed-local: ## Generate events to stdout — no AWS, no cost
	./.venv/bin/python -m ingestion.producer --duration 5 --rate 10 --dry-run

run-pipeline: ## Start one Step Functions execution by hand
	aws stepfunctions start-execution \
		--state-machine-arn $$($(TF) output -raw state_machine_arn) \
		--input '{"trigger":"manual","process_date":""}' \
		--profile $(AWS_PROFILE) --no-cli-pager

.PHONY: help venv lint fmt test api-local mcp-local package init plan apply destroy output \
        stream-down stream-up demo-up demo-down cost eks-destroy seed seed-local run-pipeline
