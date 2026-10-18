#!/usr/bin/env bash
# ==============================================================================
# Purpose: Automated deployment, observability provisioning, and Agent Registry
#          registration script for the GCP Telecom ADK Agent (Agent Runtime).
# Architecture/Context: Deploys the Telecom ADK Agent to Google Cloud Agent Runtime
#                       (Vertex AI Reasoning Engines), configures Cloud VPN / PSC
#                       connectivity to the On-Premises Apigee API Proxy, enables
#                       Agent Runtime Cloud Trace, Cloud Logging, and Cloud Monitoring
#                       metrics, and registers/verifies the agent in GCP Agent Registry.
# Dependencies/Side Effects: Requires gcloud CLI, uv, and google-agents-cli. Reads
#                            configuration from .env in TE/GCP/.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

log_info()  { printf "\033[1;34m[INFO]\033[0m  %s\n" "$*"; }
log_ok()    { printf "\033[1;32m[OK]\033[0m    %s\n" "$*"; }
log_warn()  { printf "\033[1;33m[WARN]\033[0m  %s\n" "$*"; }
log_error() { printf "\033[1;31m[ERROR]\033[0m %s\n" "$*" >&2; }

# ------------------------------------------------------------------------------
# 1. Load & Validate .env Configuration
# ------------------------------------------------------------------------------
if [[ ! -f ".env" ]]; then
  cp .env.example .env
  log_warn "Created .env from .env.example. Please review and update GOOGLE_CLOUD_PROJECT and MCP_SERVER_URL in .env before deploying."
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
MCP_SERVER_URL="${MCP_SERVER_URL:-}"
APIGEE_AUTH_MODE="${APIGEE_AUTH_MODE:-auto}"
APIGEE_VERIFY_SSL="${APIGEE_VERIFY_SSL:-true}"
ENABLE_AGENT_IDENTITY="${ENABLE_AGENT_IDENTITY:-true}"
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-EVENT_ONLY}"

if [[ -z "${GOOGLE_CLOUD_PROJECT}" || "${GOOGLE_CLOUD_PROJECT}" == "your-gcp-project-id" ]]; then
  log_error "GOOGLE_CLOUD_PROJECT is not configured in .env. Please set a valid GCP Project ID."
  exit 1
fi

if [[ -z "${MCP_SERVER_URL}" ]]; then
  log_error "MCP_SERVER_URL is not configured in .env. Please set the On-Premises Apigee Proxy URL."
  exit 1
fi

LOGS_BUCKET_NAME="${LOGS_BUCKET_NAME:-${GOOGLE_CLOUD_PROJECT}-telecom-agent-logs}"
LOGS_BUCKET_NAME="${LOGS_BUCKET_NAME#gs://}"
LOGS_BUCKET_NAME="${LOGS_BUCKET_NAME%/}"

log_info "Target GCP Project   : ${GOOGLE_CLOUD_PROJECT}"
log_info "Target GCP Region    : ${GOOGLE_CLOUD_LOCATION}"
log_info "Apigee MCP Proxy URL : ${MCP_SERVER_URL}"
log_info "Apigee Auth Mode     : ${APIGEE_AUTH_MODE}"
log_info "Telemetry Bucket     : gs://${LOGS_BUCKET_NAME}"

# ------------------------------------------------------------------------------
# 2. Verify CLI Prerequisites (gcloud, uv, agents-cli)
# ------------------------------------------------------------------------------
if ! command -v gcloud >/dev/null 2>&1; then
  log_error "gcloud CLI is not installed. Please install Google Cloud SDK first."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  log_info "Installing uv package manager..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if ! command -v agents-cli >/dev/null 2>&1; then
  log_info "Installing google-agents-cli..."
  uv tool install google-agents-cli
  export PATH="${HOME}/.local/bin:${PATH}"
fi

# ------------------------------------------------------------------------------
# 3. Enable Required GCP APIs (Agent Runtime, Agent Registry & Observability)
# ------------------------------------------------------------------------------
log_info "Enabling Agent Runtime, Agent Registry, and GCP Observability APIs..."
gcloud services enable \
  aiplatform.googleapis.com \
  agentregistry.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  cloudtrace.googleapis.com \
  telemetry.googleapis.com \
  storage.googleapis.com \
  bigquery.googleapis.com \
  bigqueryconnection.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  compute.googleapis.com \
  --project="${GOOGLE_CLOUD_PROJECT}"

log_ok "All required GCP APIs enabled."

# ------------------------------------------------------------------------------
# 4. Provision GCS Telemetry Bucket & Grant Agent Runtime IAM Roles
# ------------------------------------------------------------------------------
log_info "Ensuring GCS telemetry bucket gs://${LOGS_BUCKET_NAME} exists..."
if ! gcloud storage buckets describe "gs://${LOGS_BUCKET_NAME}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${LOGS_BUCKET_NAME}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_LOCATION}" \
    --uniform-bucket-level-access
  log_ok "Created telemetry bucket gs://${LOGS_BUCKET_NAME}"
else
  log_ok "Telemetry bucket gs://${LOGS_BUCKET_NAME} already exists."
fi

PROJECT_NUMBER="$(gcloud projects describe "${GOOGLE_CLOUD_PROJECT}" --format='value(projectNumber)')"
RE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

log_info "Granting Agent Runtime telemetry, logging, trace, monitoring, and Agent Registry IAM roles..."
for SA in "${RE_SA}" "${COMPUTE_SA}"; do
  for ROLE in \
    "roles/aiplatform.user" \
    "roles/logging.logWriter" \
    "roles/cloudtrace.agent" \
    "roles/monitoring.metricWriter" \
    "roles/storage.objectAdmin" \
    "roles/secretmanager.secretAccessor"; do
    gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
      --member="serviceAccount:${SA}" \
      --role="${ROLE}" \
      --condition=None \
      --quiet >/dev/null 2>&1 || true
  done
done

# ------------------------------------------------------------------------------
# 5. Provision Cloud Logging Metrics for Agent Runtime Monitoring
# ------------------------------------------------------------------------------
log_info "Configuring Cloud Logging log-based metrics for Agent Runtime..."

create_or_update_log_metric() {
  local metric_name="$1"
  local description="$2"
  local filter="$3"
  if gcloud logging metrics describe "${metric_name}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
    gcloud logging metrics update "${metric_name}" \
      --description="${description}" \
      --log-filter="${filter}" \
      --project="${GOOGLE_CLOUD_PROJECT}" --quiet >/dev/null
  else
    gcloud logging metrics create "${metric_name}" \
      --description="${description}" \
      --log-filter="${filter}" \
      --project="${GOOGLE_CLOUD_PROJECT}" --quiet >/dev/null
  fi
}

create_or_update_log_metric \
  "telecom_agent_mcp_tool_calls_total" \
  "Count of MCP tool calls dispatched from GCP Agent Runtime to the On-Premises Apigee proxy over Cloud VPN" \
  'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND jsonPayload.event="mcp_tool_call_completed"'

create_or_update_log_metric \
  "telecom_agent_mcp_tool_errors_total" \
  "Count of failed MCP tool calls (HTTP, JSON-RPC, or Cloud VPN connectivity errors) on GCP Agent Runtime" \
  'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND (jsonPayload.event="mcp_tool_call_http_error" OR jsonPayload.event="mcp_tool_call_rpc_error" OR jsonPayload.event="mcp_tool_call_connect_error" OR jsonPayload.event="mcp_tool_call_unexpected_error")'

create_or_update_log_metric \
  "telecom_agent_model_armor_blocks_total" \
  "Count of MCP responses blocked by Apigee Model Armor (-32001) observed by GCP Agent Runtime" \
  'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND jsonPayload.event="mcp_tool_blocked_by_model_armor"'

log_ok "Agent Runtime Cloud Logging metrics configured."

# ------------------------------------------------------------------------------
# 6. Build Environment Variable List & Deploy to GCP Agent Runtime
# ------------------------------------------------------------------------------
ENV_VARS=(
  "GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION}"
  "GOOGLE_GENAI_USE_VERTEXAI=true"
  "MODEL_NAME=${MODEL_NAME:-gemini-2.5-flash}"
  "MCP_SERVER_URL=${MCP_SERVER_URL}"
  "APIGEE_AUTH_MODE=${APIGEE_AUTH_MODE}"
  "APIGEE_VERIFY_SSL=${APIGEE_VERIFY_SSL}"
  "APIGEE_API_KEY_HEADER=${APIGEE_API_KEY_HEADER:-x-apikey}"
  "MCP_DEPLOYMENT_TARGET=${MCP_DEPLOYMENT_TARGET:-on_prem}"
  "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true"
  "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT}"
  "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=${ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS:-false}"
  "OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental"
  "LOGS_BUCKET_NAME=${LOGS_BUCKET_NAME}"
  "LOG_LEVEL=${LOG_LEVEL:-INFO}"
)

[[ -n "${APIGEE_API_KEY:-}" ]] && ENV_VARS+=("APIGEE_API_KEY=${APIGEE_API_KEY}")
[[ -n "${MCP_AUTH_TOKEN:-}" ]] && ENV_VARS+=("MCP_AUTH_TOKEN=${MCP_AUTH_TOKEN}")
[[ -n "${APIGEE_BEARER_TOKEN:-}" ]] && ENV_VARS+=("APIGEE_BEARER_TOKEN=${APIGEE_BEARER_TOKEN}")
[[ -n "${APIGEE_OIDC_AUDIENCE:-}" ]] && ENV_VARS+=("APIGEE_OIDC_AUDIENCE=${APIGEE_OIDC_AUDIENCE}")
[[ -n "${APIGEE_BASIC_USER:-}" ]] && ENV_VARS+=("APIGEE_BASIC_USER=${APIGEE_BASIC_USER}")
[[ -n "${APIGEE_BASIC_PASSWORD:-}" ]] && ENV_VARS+=("APIGEE_BASIC_PASSWORD=${APIGEE_BASIC_PASSWORD}")
[[ -n "${APIGEE_CUSTOM_HEADER_NAME:-}" ]] && ENV_VARS+=("APIGEE_CUSTOM_HEADER_NAME=${APIGEE_CUSTOM_HEADER_NAME}")
[[ -n "${APIGEE_CUSTOM_HEADER_VALUE:-}" ]] && ENV_VARS+=("APIGEE_CUSTOM_HEADER_VALUE=${APIGEE_CUSTOM_HEADER_VALUE}")

IFS=',' eval 'ENV_VARS_CSV="${ENV_VARS[*]}"'

DEPLOY_CMD=(
  agents-cli deploy
  --project "${GOOGLE_CLOUD_PROJECT}"
  --region "${GOOGLE_CLOUD_LOCATION}"
  --no-confirm-project
  --update-env-vars "${ENV_VARS_CSV}"
)

if [[ "${ENABLE_AGENT_IDENTITY,,}" == "true" ]]; then
  DEPLOY_CMD+=(--agent-identity)
fi

if [[ -n "${NETWORK_ATTACHMENT:-}" ]]; then
  log_info "Binding Agent Runtime to Cloud VPN / PSC Network Attachment: ${NETWORK_ATTACHMENT}"
  DEPLOY_CMD+=(--network-attachment "${NETWORK_ATTACHMENT}")
  [[ -n "${DNS_PEERING_DOMAIN:-}" ]] && DEPLOY_CMD+=(--dns-peering-domain "${DNS_PEERING_DOMAIN}")
  [[ -n "${DNS_PEERING_PROJECT:-}" ]] && DEPLOY_CMD+=(--dns-peering-project "${DNS_PEERING_PROJECT}")
  [[ -n "${DNS_PEERING_NETWORK:-}" ]] && DEPLOY_CMD+=(--dns-peering-network "${DNS_PEERING_NETWORK}")
fi

log_info "Deploying Telecom ADK Agent to GCP Agent Runtime..."
"${DEPLOY_CMD[@]}"

# ------------------------------------------------------------------------------
# 7. Verify Agent Registry Registration & Display Gemini Enterprise Details
# ------------------------------------------------------------------------------
REASONING_ENGINE_ID=""
if [[ -f "deployment_metadata.json" ]]; then
  REASONING_ENGINE_ID="$(python3 -c 'import json; print(json.load(open("deployment_metadata.json")).get("remote_agent_runtime_id", ""))' 2>/dev/null || true)"
fi

log_info "Verifying registration in GCP Agent Registry (${GOOGLE_CLOUD_LOCATION})..."
if gcloud alpha agent-registry agents list \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --location="${GOOGLE_CLOUD_LOCATION}" >/dev/null 2>&1; then
  gcloud alpha agent-registry agents list \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_LOCATION}"
elif gcloud agent-registry agents list \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --location="${GOOGLE_CLOUD_LOCATION}" >/dev/null 2>&1; then
  gcloud agent-registry agents list \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_LOCATION}"
else
  log_info "Agent Runtime automatically registers deployed Reasoning Engines in GCP Agent Registry."
  log_info "Console URL: https://console.cloud.google.com/vertex-ai/agents/agent-registry?project=${GOOGLE_CLOUD_PROJECT}"
fi

printf "\n================================================================================\n"
log_ok "GCP Agent Runtime Deployment & Observability Setup Complete!"
printf "================================================================================\n"
printf "  Reasoning Engine Resource ID : %s\n" "${REASONING_ENGINE_ID:-<see deployment_metadata.json>}"
printf "  Agent Registry Console       : https://console.cloud.google.com/vertex-ai/agents/agent-registry?project=%s\n" "${GOOGLE_CLOUD_PROJECT}"
printf "  Agent Runtime Console        : https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=%s\n" "${GOOGLE_CLOUD_PROJECT}"
printf "  Cloud Trace Explorer         : https://console.cloud.google.com/traces/list?project=%s\n" "${GOOGLE_CLOUD_PROJECT}"
printf "  Cloud Logging Explorer       : https://console.cloud.google.com/logs/query;query=resource.type%%3D%%22aiplatform.googleapis.com%%2FReasoningEngine%%22?project=%s\n" "${GOOGLE_CLOUD_PROJECT}"
printf "================================================================================\n"
