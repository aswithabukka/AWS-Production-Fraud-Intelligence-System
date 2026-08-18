# ECR repositories, ECS Fargate service, and the ALB.
#
# COST, in order of how much it matters:
#   - ALB:      ~$16/month + LCU, billed the moment it exists. `desired_count = 0` does
#               NOT stop it. `enable_alb = false` is the lever that does.
#   - Fargate:  per-second while a task runs. desired_count defaults to 0, so the steady
#               state is zero.
#   - ECR:      per-GB stored. The lifecycle policy keeps it to a handful of images.
#
# The ALB is the trap here: people set desired_count to 0, see no tasks, and assume the
# stack costs nothing while a load balancer quietly bills all month.

# ---------------------------------------------------------------------------- ECR

resource "aws_ecr_repository" "repo" {
  for_each = toset(var.repositories)

  name                 = "${var.name_prefix}/${each.value}"
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_delete_repositories

  image_scanning_configuration {
    # Free, and there is no argument for turning it off.
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "repo" {
  for_each = aws_ecr_repository.repo

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 5 most recent tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "sha", "latest", "main"]
          countType     = "imageCountMoreThan"
          countNumber   = 5
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged layers after a day — CI produces these on every build"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
    ]
  })
}

# -------------------------------------------------------------------------- roles

data "aws_iam_policy_document" "execution_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# The EXECUTION role pulls the image and writes logs. It is not the task's identity —
# keeping them separate means a compromised container cannot pull arbitrary ECR images.
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution-role"
  assume_role_policy = data.aws_iam_policy_document.execution_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------------- networking

resource "aws_security_group" "alb" {
  count = var.enable_alb ? 1 : 0

  name        = "${var.name_prefix}-alb"
  description = "Public HTTP to the ALB"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP from the allowed CIDRs"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    # Defaults to your own IP rather than 0.0.0.0/0 — a demo endpoint has no reason to
    # accept traffic from the entire internet.
    cidr_blocks = var.allowed_ingress_cidrs
  }

  egress {
    description = "To the tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "task" {
  name        = "${var.name_prefix}-task"
  description = "Fargate tasks: inbound from the ALB only"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = var.enable_alb ? [1] : []

    content {
      description     = "Application port, from the ALB only"
      from_port       = var.container_port
      to_port         = var.container_port
      protocol        = "tcp"
      security_groups = [aws_security_group.alb[0].id]
    }
  }

  # Without an ALB, demo traffic hits the task's public IP directly — still restricted
  # to the allowed CIDRs (your own IP), never 0.0.0.0/0.
  dynamic "ingress" {
    for_each = var.enable_alb ? [] : [1]

    content {
      description = "Application port, direct to the task, allowed CIDRs only"
      from_port   = var.container_port
      to_port     = var.container_port
      protocol    = "tcp"
      cidr_blocks = var.allowed_ingress_cidrs
    }
  }

  # Egress to AWS APIs — either via the internet gateway (public_tasks mode) or via the
  # interface endpoints (endpoints mode). Both are HTTPS.
  egress {
    description = "HTTPS to AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------------------- ALB

resource "aws_lb" "main" {
  count = var.enable_alb ? 1 : 0

  name               = "${var.name_prefix}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb[0].id]
  subnets            = var.public_subnet_ids

  # Off: access logs land in S3 per request and this is a demo endpoint. Turn on if you
  # want the request-level story for the README.
  enable_deletion_protection = false
  idle_timeout               = 120 # the graph can take a while; 60s would cut answers off
}

resource "aws_lb_target_group" "api" {
  count = var.enable_alb ? 1 : 0

  name        = "${var.name_prefix}-api"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip" # Fargate awsvpc tasks register by IP, not instance

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 15
}

resource "aws_lb_listener" "http" {
  count = var.enable_alb ? 1 : 0

  load_balancer_arn = aws_lb.main[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }
}

# ---------------------------------------------------------------------- ECS service

resource "aws_ecs_cluster" "main" {
  name = "${var.name_prefix}-cluster"

  setting {
    # Container Insights is per-metric and adds up quickly. The dashboard is built on
    # the application's own custom metrics instead.
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.name_prefix}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"

  # The smallest Fargate size available. The workload is IO-bound waiting on Athena and
  # Bedrock; more vCPU would buy nothing but a bigger per-second rate.
  cpu    = var.task_cpu
  memory = var.task_memory

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = var.task_role_arn

  runtime_platform {
    cpu_architecture        = "ARM64" # ~20% cheaper per vCPU-second than X86_64
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = var.api_image
      essential = true

      portMappings = [{ containerPort = var.container_port, protocol = "tcp" }]

      environment = [for k, v in var.environment : { name = k, value = v }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "api"
        }
      }

      healthCheck = {
        command = [
          "CMD-SHELL",
          "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${var.container_port}/health', timeout=4).status==200 else 1)\"",
        ]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }

      readonlyRootFilesystem = true
      user                   = "10001:10001"

      # /tmp must stay writable with a read-only root filesystem — Python writes there.
      mountPoints = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    }
  ])

  volume {
    name = "tmp"
  }
}

resource "aws_ecs_service" "api" {
  name            = "${var.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  launch_type     = "FARGATE"

  # ZERO by default. The service exists so a demo is one variable away, and costs nothing
  # until you ask for a task.
  desired_count = var.desired_count

  network_configuration {
    subnets          = var.task_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = var.assign_public_ip
  }

  dynamic "load_balancer" {
    for_each = var.enable_alb ? [1] : []

    content {
      target_group_arn = aws_lb_target_group.api[0].arn
      container_name   = "api"
      container_port   = var.container_port
    }
  }

  # Without this, a task that fails its health check for the first 60s while the Python
  # process starts gets killed and retried forever.
  health_check_grace_period_seconds = var.enable_alb ? 60 : null

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    # CI updates the image and therefore the task definition. Terraform should not fight
    # the deployment pipeline over which revision is current.
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]
}
