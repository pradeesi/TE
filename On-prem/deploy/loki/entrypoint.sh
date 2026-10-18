#!/bin/sh
# ==============================================================================
# Purpose: Loki entrypoint for the On-Premises Ubuntu VM deployment.
# Architecture/Context:
#   - Validates LOKI_RETENTION_PERIOD and starts Grafana Loki 3.0.0 with
#     filesystem storage under `/tmp/loki` (persisted via Docker volume `loki_data`).
# Dependencies/Side Effects:
#   - Execs `/usr/bin/loki` as PID 1 so SIGTERM triggers a clean WAL flush on VM
#     restart or container stop.
# ==============================================================================
set -eu

CONFIG="/etc/loki/loki-config.yml"

log() {
  echo "{\"severity\":\"$1\",\"component\":\"loki-entrypoint\",\"message\":\"$2\"}"
}

if [ -n "${LOKI_RETENTION_PERIOD:-}" ] && ! echo "${LOKI_RETENTION_PERIOD}" | grep -Eq '^[0-9]+[smhdwy]$'; then
  log "ERROR" "LOKI_RETENTION_PERIOD must be a duration such as 744h or 31d."
  exit 1
fi

mkdir -p /tmp/loki/chunks /tmp/loki/rules /tmp/loki/compactor
log "INFO" "Starting Grafana Loki with persistent filesystem storage under /tmp/loki."

exec /usr/bin/loki -config.file="${CONFIG}" -config.expand-env=true "$@"
