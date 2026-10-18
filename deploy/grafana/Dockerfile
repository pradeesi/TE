# ==============================================================================
# Purpose: Container image for Grafana (On-Premises Telecom MCP Security and
#          AI Agent Observability dashboards).
# Architecture/Context:
#   - Auto-provisions Prometheus (`http://prometheus:9090`) and Loki
#     (`http://loki:3100`) datasources and preloads the observability dashboards.
# Dependencies/Side Effects:
#   - Admin credentials are provided at runtime via `GF_SECURITY_ADMIN_USER` and
#     `GF_SECURITY_ADMIN_PASSWORD` from `.env`.
#   - Pre-creates `/tmp/grafana` owned by `472:0` so the named Docker volume
#     (`grafana_data`) persists state across Ubuntu VM restarts.
# ==============================================================================
FROM grafana/grafana:10.4.0

USER root
RUN apk add --no-cache curl jq && \
    mkdir -p /tmp/grafana/plugins /tmp/grafana/logs && \
    chown -R 472:0 /tmp/grafana
COPY deploy/grafana/provisioning /etc/grafana/provisioning
COPY deploy/grafana/dashboards /var/lib/grafana/dashboards
COPY deploy/grafana/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh
USER 472

EXPOSE 3000

ENV GRAFANA_DATASOURCE_AUTH=none
ENV GF_USERS_ALLOW_SIGN_UP=false
ENV GF_AUTH_ANONYMOUS_ENABLED=false
ENV GF_ANALYTICS_REPORTING_ENABLED=false
ENV GF_ANALYTICS_CHECK_FOR_UPDATES=false
ENV GF_PATHS_DATA=/tmp/grafana
ENV GF_PATHS_LOGS=/tmp/grafana/logs
ENV GF_PATHS_PLUGINS=/tmp/grafana/plugins

ENTRYPOINT ["/entrypoint.sh"]
