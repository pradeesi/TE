# ==============================================================================
# Purpose: Container image for Prometheus on the On-Premises Ubuntu VM.
# Architecture/Context:
#   - Scrapes `/metrics` from `mcp-server:8001` every 10 seconds over the
#     internal Docker Compose bridge network (`telecom-net`).
# Dependencies/Side Effects:
#   - Runs as `nobody`; pre-creates `/tmp/prometheus` owned by `nobody` so the
#     named Docker volume (`prometheus_data`) retains metrics across VM restarts.
# ==============================================================================
FROM prom/prometheus:v2.50.1

COPY deploy/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY deploy/prometheus/entrypoint.sh /entrypoint.sh

USER root
RUN mkdir -p /tmp/prometheus && \
    chown -R nobody:nobody /tmp/prometheus && \
    chmod 0755 /entrypoint.sh && \
    chmod 0644 /etc/prometheus/prometheus.yml
USER nobody

EXPOSE 9090

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--storage.tsdb.path=/tmp/prometheus", "--storage.tsdb.retention.time=3d", "--storage.tsdb.retention.size=1GB"]
