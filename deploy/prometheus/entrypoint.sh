#!/bin/sh
# ==============================================================================
# Purpose: Prometheus entrypoint for the On-Premises Ubuntu VM deployment.
# Architecture/Context:
#   - Starts Prometheus scraping `mcp-server:8001/metrics` on the internal
#     Docker network (`telecom-net`).
# Dependencies/Side Effects:
#   - Execs `/bin/prometheus` as PID 1 listening on `${PORT:-9090}`.
# ==============================================================================
set -eu

CONFIG="/etc/prometheus/prometheus.yml"

echo "{\"severity\":\"INFO\",\"component\":\"prometheus-entrypoint\",\"message\":\"Starting Prometheus scraping mcp-server:8001.\"}"

exec /bin/prometheus \
  --config.file="${CONFIG}" \
  --web.listen-address=":${PORT:-9090}" \
  "$@"
