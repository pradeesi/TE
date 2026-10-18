# ==============================================================================
# Purpose: Production OCI container image for the On-Premises Telecom MCP Server
#          and Customer Care CRM Application.
# Architecture/Context:
#   - Built once by Docker Compose and shared by two on-premises containers:
#       1. mcp-server : command  python3 -m src.mcp.server   (port 8001)
#       2. crm-app    : command  python3 -m src.crm.app      (port 8000, default CMD)
#   - Connects to the on-premises PostgreSQL 16 database (`postgres:5432`) and
#     streams structured audit logs to Grafana Loki (`loki:3100`).
# Dependencies/Side Effects:
#   - Runs as an unprivileged non-root user (`telecom`, uid 10001).
#   - No secrets are baked into the image; all credentials and endpoints are
#     injected at runtime via `.env` and `docker-compose.yml`.
# ==============================================================================

FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output for real-time logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install curl for container HEALTHCHECK probes in docker-compose
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python requirements first to maximize Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code and packaging metadata
COPY pyproject.toml .
COPY src/ ./src/

# Create unprivileged application user for security hardening
RUN groupadd -g 10001 telecom && \
    useradd -u 10001 -g telecom -s /usr/sbin/nologin -m telecom && \
    chown -R telecom:telecom /app

USER 10001:10001

EXPOSE 8000 8001

# Default command starts the CRM console; MCP service overrides with: python3 -m src.mcp.server
CMD ["python3", "-m", "src.crm.app"]
