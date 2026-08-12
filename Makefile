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

venv: ## Create the local virtualenv and install dev deps
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements-dev.txt

lint: ## ruff + terraform fmt check
	./.venv/bin/ruff check .
	./.venv/bin/ruff format --check .
	terraform fmt -recursive -check terraform/

fmt: ## Auto-format python + terraform
	./.venv/bin/ruff format .
	./.venv/bin/ruff check --fix .
	terraform fmt -recursive terraform/

test: ## Run pytest (no AWS calls)
	./.venv/bin/pytest -q

# ---------------------------------------------------------------- terraform

init: ## terraform init
	$(TF) init

plan: ## terraform plan  (ALWAYS run before apply)
	$(TF) plan -out=tfplan

apply: ## terraform apply  (human-run only — review the plan first)
	@echo "Review the plan above. Resources with an idle cost in this stack:"
	@echo "  - Kinesis on-demand stream (~\$$0.036/stream-hour). Run 'make stream-down' when idle."
	@read -p "Type 'apply' to continue: " ans; [ "$$ans" = "apply" ] || exit 1
	$(TF) apply tfplan

destroy: ## Tear down EVERYTHING in the dev env
	@read -p "This destroys the whole dev stack. Type 'destroy' to continue: " ans; [ "$$ans" = "destroy" ] || exit 1
	$(TF) destroy

output: ## Show terraform outputs
	$(TF) output

# ---------------------------------------------------------------- cost levers

stream-down: ## Destroy just the Kinesis stream + Firehose, keep the lake queryable
	$(TF) apply -var 'enable_stream=false'

stream-up: ## Recreate the Kinesis stream + Firehose for a demo session
	$(TF) apply -var 'enable_stream=true'

cost: ## Month-to-date spend for this project, grouped by service
	@aws ce get-cost-and-usage \
		--time-period Start=$$(date -v1d +%Y-%m-%d),End=$$(date -v+1d +%Y-%m-%d) \
		--granularity MONTHLY --metrics UnblendedCost \
		--group-by Type=DIMENSION,Key=SERVICE \
		--filter '{"Tags":{"Key":"Project","Values":["fraud-lake"]}}' \
		--profile $(AWS_PROFILE) --output table

# ---------------------------------------------------------------- data

seed: ## Run the producer against the live Kinesis stream (60s, ~50 eps)
	./.venv/bin/python -m ingestion.producer --duration 60 --rate 50 \
		--stream $$($(TF) output -raw kinesis_stream_name)

seed-local: ## Generate events to stdout — no AWS, no cost
	./.venv/bin/python -m ingestion.producer --duration 5 --rate 10 --dry-run

demo: ## Placeholder — end-to-end demo script (slice 1c)
	@echo "Not implemented until slice 1c."

.PHONY: help venv lint fmt test init plan apply destroy output stream-down stream-up cost seed seed-local demo
