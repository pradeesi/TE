#!/bin/sh
# ==============================================================================
# Purpose: Grafana entrypoint for the On-Premises Ubuntu VM deployment.
# Architecture/Context:
#   - Validates that admin credentials (`GF_SECURITY_ADMIN_USER` and
#     `GF_SECURITY_ADMIN_PASSWORD`) are explicitly configured before starting
#     Grafana with pre-provisioned Prometheus and Loki datasources.
# Dependencies/Side Effects:
#   - Execs `/run.sh` as PID 1 so Grafana receives container lifecycle signals.
# ==============================================================================
set -eu

log() {
  jq -cn --arg severity "$1" --arg message "$2" \
    '{severity: $severity, component: "grafana-entrypoint", message: $message}'
}

if [ -z "${GF_SECURITY_ADMIN_USER:-}" ] || [ -z "${GF_SECURITY_ADMIN_PASSWORD:-}" ]; then
  log "ERROR" "GF_SECURITY_ADMIN_USER and GF_SECURITY_ADMIN_PASSWORD must both be set."
  exit 1
fi

mkdir -p "${GF_PATHS_DATA:-/tmp/grafana}" "${GF_PATHS_PLUGINS:-/tmp/grafana/plugins}" "${GF_PATHS_LOGS:-/tmp/grafana/logs}"
log "INFO" "Starting Grafana with pre-provisioned Prometheus and Loki datasources."

exec /run.sh "$@"
