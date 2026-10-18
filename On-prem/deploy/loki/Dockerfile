# ==============================================================================
# Purpose: Container image for Grafana Loki 3.x (On-Premises Audit & PII Log Store).
# Architecture/Context:
#   - Single-binary Loki 3.0.0 running on the Customer Data Center Ubuntu VM.
#   - Receives full raw MCP tool audit records from `mcp-server` and `crm-app`,
#     plus masked response logs from the customer's Apigee Hybrid runtime.
# Dependencies/Side Effects:
#   - Runs as non-root uid 10001; pre-creates `/tmp/loki` directories with
#     uid 10001 ownership so Docker named volumes (`loki_data`) inherit write
#     permissions across Ubuntu VM reboots.
# ==============================================================================
FROM grafana/loki:3.0.0

COPY deploy/loki/loki-config.yml /etc/loki/loki-config.yml
COPY deploy/loki/entrypoint.sh /entrypoint.sh

USER root
RUN mkdir -p /tmp/loki/chunks /tmp/loki/rules /tmp/loki/compactor && \
    chown -R 10001:10001 /tmp/loki && \
    chmod 0755 /entrypoint.sh && \
    chmod 0644 /etc/loki/loki-config.yml
USER 10001

EXPOSE 3100

ENTRYPOINT ["/entrypoint.sh"]
CMD []
