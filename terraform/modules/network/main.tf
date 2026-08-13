# VPC for the containerised services. NO NAT GATEWAY, in either mode.
#
# ============================ THE COST DECISION ================================
#
# The standard advice — "use VPC endpoints instead of a NAT gateway" — is right about the
# NAT and incomplete about the arithmetic. At current us-east-1 pricing:
#
#   NAT gateway ................................ ~$0.045/hr  = ~$32/month + per-GB
#   Interface endpoint ......................... ~$0.01/hr/AZ = ~$7.30/month/AZ
#
# This service needs ECR (x2), CloudWatch Logs, Athena, Glue, Bedrock runtime, Bedrock
# agent runtime, STS, CloudWatch, and Step Functions. Across 2 AZs that is roughly
# **$130/month** — four times the NAT gateway it was meant to avoid.
#
# Endpoints win at scale, where per-GB NAT processing dominates and the traffic never
# leaves the AWS network. They lose badly on a portfolio project that runs for an hour a
# week. Repeating the rule without checking which regime you are in is exactly the kind of
# cargo-culting this project is meant to avoid.
#
# Hence two modes:
#
#   networking_mode = "public_tasks"  (DEFAULT, $0/month)
#       Fargate tasks run in public subnets with a public IP and a security group that
#       permits NO inbound traffic except from the ALB. Egress to AWS APIs goes over the
#       internet gateway, which is free. The task is not reachable from the internet; it
#       simply has a route out. For a demo workload this is the correct cost answer, and
#       being able to explain why is the point.
#
#   networking_mode = "endpoints"     (~$130/month WHILE UP — tear down same day)
#       Tasks run in private subnets with no route to the internet at all, reaching AWS
#       services through interface endpoints. This is the production-shaped answer and the
#       one worth screenshotting for the architecture diagram.
#
# Either way there is no NAT gateway, and the S3 gateway endpoint (which is free) is
# always created.
# ===============================================================================

locals {
  use_endpoints = var.networking_mode == "endpoints"

  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Interface endpoints required when tasks have no internet route. Each one bills per
  # hour per AZ — the list length is the monthly bill.
  interface_services = [
    "ecr.api",               # pull image manifests
    "ecr.dkr",               # pull image layers
    "logs",                  # container logs
    "athena",                # query_lakehouse
    "glue",                  # schema introspection
    "bedrock-runtime",       # Converse
    "bedrock-agent-runtime", # Knowledge Base retrieval
    "sts",                   # task role credentials
    "monitoring",            # PutMetricData
    "states",                # pipeline_status
  ]
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # required for interface endpoints to resolve

  tags = { Name = "${var.name_prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name_prefix}-igw" }
}

# ------------------------------------------------------------------ public subnets
# Always created: the ALB must live in public subnets regardless of where tasks run.

resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.name_prefix}-public-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.name_prefix}-public" }
}

resource "aws_route_table_association" "public" {
  count = var.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ----------------------------------------------------------------- private subnets
# Only created in endpoints mode. A private subnet with no NAT and no endpoints is a
# subnet whose tasks cannot pull their own image.

resource "aws_subnet" "private" {
  count = local.use_endpoints ? var.az_count : 0

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
  availability_zone = local.azs[count.index]

  tags = { Name = "${var.name_prefix}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "private" {
  count = local.use_endpoints ? 1 : 0

  vpc_id = aws_vpc.main.id

  # Deliberately no 0.0.0.0/0 route. Everything reachable goes through an endpoint.
  tags = { Name = "${var.name_prefix}-private" }
}

resource "aws_route_table_association" "private" {
  count = local.use_endpoints ? var.az_count : 0

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# ------------------------------------------------------------------ S3 gateway endpoint
# FREE, and created in both modes. Gateway endpoints have no hourly charge and no per-GB
# charge — traffic to S3 stays on the AWS network and off the internet gateway. There is
# no configuration in which this is not worth having.

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"

  route_table_ids = concat(
    [aws_route_table.public.id],
    local.use_endpoints ? [aws_route_table.private[0].id] : [],
  )

  tags = { Name = "${var.name_prefix}-s3-endpoint" }
}

# ------------------------------------------------------------- interface endpoints

resource "aws_security_group" "endpoints" {
  count = local.use_endpoints ? 1 : 0

  name        = "${var.name_prefix}-endpoints"
  description = "HTTPS from inside the VPC to the interface endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from within the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = { Name = "${var.name_prefix}-endpoints" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.use_endpoints ? toset(local.interface_services) : toset([])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true

  tags = { Name = "${var.name_prefix}-${each.value}" }
}
