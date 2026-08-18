# Multi-stage build for the FastAPI service.
#
# Stage 1 compiles wheels; stage 2 copies only the installed packages and the source.
# The build toolchain never reaches the runtime image, which keeps it small and removes
# a compiler from the attack surface of a container that talks to a language model.

# ---------------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# HTTPS mirrors: transparent proxies corrupt http downloads ("Hash Sum mismatch").
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# --user puts everything under /root/.local, a single directory to copy forward.
RUN pip install --user -r requirements.txt

# ---------------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

# Non-root. A container that never needs to write outside /tmp has no reason to run as
# root, and ECS/EKS security policies increasingly refuse images that do.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/home/app/.local/bin:$PATH \
    PORT=8000

WORKDIR /app

COPY --from=builder --chown=app:app /root/.local /home/app/.local

COPY --chown=app:app agents/ ./agents/
COPY --chown=app:app api/ ./api/
COPY --chown=app:app ml/ ./ml/
COPY --chown=app:app mcp_server/ ./mcp_server/

USER app

EXPOSE 8000

# Hits /health, which deliberately does not call Bedrock — a health check that invokes a
# model would bill on every probe. At a 30-second interval that is ~86,400 model calls a
# month for information a config check already provides.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# One worker: the workload is IO-bound on Athena and Bedrock, and Fargate tasks are sized
# small on purpose. Scale with task count, not with workers inside a 0.25 vCPU task.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
