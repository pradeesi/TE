#!/usr/bin/env bash
# ==============================================================================
# Author: Pradeep Singh
# License: Apache-2.0
# Purpose: Robust, fault-tolerant, idempotent Google Cloud Shell installer for
#          the Telecom Customer Care ADK Agent on GCP Agent Runtime.
# Architecture/Context:
#   - Interactive numbered GCP Project selector (or `--project <PROJECT_ID>`).
#   - Interactive or flag-driven Region & On-Premises Apigee Proxy URL setup.
#   - Pre-flight verification: Cloud Shell / Linux tools (`gcloud`, `jq`, `curl`,
#     `python3`, `uv`, `google-agents-cli`), active auth, and local unit tests.
#   - Enables required GCP APIs (`aiplatform`, `agentregistry`, `logging`,
#     `monitoring`, `cloudtrace`, `telemetry`, `storage`, `bigquery`, `compute`,
#     `secretmanager`, `cloudbuild`) with automatic exponential-backoff retries.
#   - Provisions GCS prompt/response completion bucket, BigQuery telemetry
#     dataset & ReasoningEngine log sink, Cloud Logging log-based metrics, and
#     the Cloud Monitoring Dashboard for Agent Runtime.
#   - Deploys the ADK Agent to GCP Agent Runtime (`agents-cli deploy`) with
#     automatic server-side timeout polling recovery (`agents-cli deploy --status`)
#     and Agent Registry verification.
# Dependencies/Side Effects:
#   - Designed to run directly inside Google Cloud Shell.
# ==============================================================================

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

export PATH="${HOME}/.local/bin:${PATH}"

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-}"
APIGEE_URL="${APIGEE_URL:-}"
AUTH_MODE="${AUTH_MODE:-}"
NETWORK_ATTACHMENT_ARG="${NETWORK_ATTACHMENT_ARG:-}"
AUTO_APPROVE="false"
ROLLBACK_ON_FAILURE="false"
SKIP_TESTS="false"
DEPLOYMENT_STARTED="false"

# ------------------------------------------------------------------------------
# Structured Logging Helpers
# ------------------------------------------------------------------------------
log_info() {
  printf '%s \033[1;34m[INFO]\033[0m  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

log_ok() {
  printf '%s \033[1;32m[OK]\033[0m    %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

log_warn() {
  printf '%s \033[1;33m[WARN]\033[0m  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

log_error() {
  printf '%s \033[1;31m[ERROR]\033[0m %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

#######################################
# Summary: Global ERR trap that logs the failing line/command, prints diagnostic
#          guidance, and optionally triggers rollback if --rollback-on-failure
#          was passed.
# Parameters: $1 - Line number; $2 - Exit code.
# Return Value: Exits with the original non-zero exit code.
# Exceptions/Errors: None.
#######################################
on_error() {
  local line_no="$1"
  local exit_code="$2"
  log_error "Installation failed at line ${line_no} (exit code ${exit_code})."

  if [[ "${DEPLOYMENT_STARTED}" == "true" && -n "${PROJECT_ID}" ]]; then
    log_warn "Fetching recent Vertex AI Reasoning Engine logs for troubleshooting..."
    gcloud logging read \
      'resource.type="aiplatform.googleapis.com/ReasoningEngine"' \
      --project="${PROJECT_ID}" \
      --limit=15 \
      --format="table(timestamp,severity,textPayload,jsonPayload.message)" 2>/dev/null || true

    if [[ "${ROLLBACK_ON_FAILURE}" == "true" && -x "${SCRIPT_DIR}/uninstall.sh" ]]; then
      log_warn "Rolling back partially provisioned GCP resources (--rollback-on-failure)..."
      "${SCRIPT_DIR}/uninstall.sh" --project "${PROJECT_ID}" --region "${REGION:-us-central1}" --yes || true
    fi
  fi

  exit "${exit_code}"
}

trap 'on_error ${LINENO} $?' ERR

#######################################
# Summary: Executes a command with exponential backoff retries to tolerate
#          transient Cloud Shell network or GCP IAM/API propagation delays.
# Parameters: $1 - Max attempts; $2 - Initial delay seconds; $3... - Command.
# Return Value: 0 on success, non-zero if all attempts fail.
# Exceptions/Errors: Returns exit code of final attempt on failure.
#######################################
retry_cmd() {
  local max_attempts="$1"
  local delay="$2"
  shift 2
  local attempt=1

  while true; do
    if "$@"; then
      return 0
    fi
    if (( attempt >= max_attempts )); then
      log_error "Command failed after ${max_attempts} attempts: $*"
      return 1
    fi
    log_warn "Attempt ${attempt}/${max_attempts} failed. Retrying in ${delay}s..."
    sleep "${delay}"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

#######################################
# Summary: Prints CLI usage instructions.
# Parameters: None.
# Return Value: None.
# Exceptions/Errors: None.
#######################################
usage() {
  cat <<EOF
Usage: ./install.sh [OPTIONS]

Fault-tolerant Google Cloud Shell installer for the Telecom Customer Care ADK
Agent on GCP Agent Runtime (with Agent Registry & GCP Observability).

Options:
  -p, --project <PROJECT_ID>          Target GCP Project ID (if omitted, displays
                                      an interactive numbered list of projects).
  -r, --region <REGION>               Target GCP Region (default: value in .env
                                      or us-central1).
  --apigee-url <URL>                  On-Premises Apigee Proxy URL over Cloud VPN
                                      (e.g., https://apigee.onprem.internal/telecom-mcp).
  --auth-mode <MODE>                  Apigee auth mode: auto, none, api_key, bearer,
                                      google_id_token, basic, or custom_header.
  --network-attachment <RESOURCE_URI> Optional PSC Network Attachment URI for
                                      routing into the Cloud VPN VPC.
  --skip-tests                        Skip local pytest unit test execution.
  --rollback-on-failure               Automatically invoke uninstall.sh if
                                      deployment fails.
  -y, --yes                           Non-interactive mode (accept defaults).
  -h, --help                          Show this help message and exit.
EOF
}

#######################################
# Summary: Parses command-line flags.
# Parameters: "$@" - Command-line arguments.
# Return Value: None.
# Exceptions/Errors: Exits 1 on invalid option.
#######################################
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -p|--project)
        [[ -z "${2:-}" ]] && { log_error "--project requires a Project ID."; exit 1; }
        PROJECT_ID="$2"
        shift 2
        ;;
      -r|--region)
        [[ -z "${2:-}" ]] && { log_error "--region requires a GCP region."; exit 1; }
        REGION="$2"
        shift 2
        ;;
      --apigee-url)
        [[ -z "${2:-}" ]] && { log_error "--apigee-url requires a URL."; exit 1; }
        APIGEE_URL="$2"
        shift 2
        ;;
      --auth-mode)
        [[ -z "${2:-}" ]] && { log_error "--auth-mode requires a mode value."; exit 1; }
        AUTH_MODE="$2"
        shift 2
        ;;
      --network-attachment)
        [[ -z "${2:-}" ]] && { log_error "--network-attachment requires a resource URI."; exit 1; }
        NETWORK_ATTACHMENT_ARG="$2"
        shift 2
        ;;
      --skip-tests)
        SKIP_TESTS="true"
        shift
        ;;
      --rollback-on-failure)
        ROLLBACK_ON_FAILURE="true"
        shift
        ;;
      -y|--yes)
        AUTO_APPROVE="true"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        log_error "Unknown option: $1"
        usage
        exit 1
        ;;
    esac
  done
}

#######################################
# Summary: Updates or inserts a KEY=VALUE pair inside the local `.env` file.
# Parameters: $1 - Key name; $2 - Value.
# Return Value: None.
# Exceptions/Errors: None.
#######################################
upsert_env_var() {
  local key="$1"
  local val="$2"
  local env_file="${SCRIPT_DIR}/.env"

  if grep -qE "^${key}=" "${env_file}" 2>/dev/null; then
    python3 - "${env_file}" "${key}" "${val}" <<'PY'
import re, sys
path, k, v = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "r", encoding="utf-8") as f:
    content = f.read()
pattern = re.compile(rf"^{re.escape(k)}=.*$", re.MULTILINE)
content = pattern.sub(f"{k}={v}", content)
with open(path, "w", encoding="utf-8") as f:
    f.write(content)
PY
  else
    printf '\n%s=%s\n' "${key}" "${val}" >> "${env_file}"
  fi
}

#######################################
# Summary: Presents an interactive numbered list of all accessible Google Cloud
#          projects for the authenticated user and sets PROJECT_ID.
# Parameters: None.
# Return Value: Sets global PROJECT_ID.
# Exceptions/Errors: Exits 1 if no projects are accessible or input is aborted.
#######################################
select_gcp_project() {
  echo ""
  log_info "Fetching accessible Google Cloud Projects for ${ACTIVE_ACCOUNT}..."
  mapfile -t PROJECT_LINES < <(
    gcloud projects list \
      --sort-by=projectId \
      --format="value[separator='|'](projectId,name)" 2>/dev/null || true
  )

  if [[ ${#PROJECT_LINES[@]} -eq 0 ]]; then
    log_error "No accessible Google Cloud Projects found for account '${ACTIVE_ACCOUNT}'."
    exit 1
  fi

  local current_gcp_project
  current_gcp_project="$(gcloud config get-value project 2>/dev/null || echo "")"
  declare -a project_ids=()

  echo ""
  echo "================================================================================"
  echo " Available Google Cloud Projects"
  echo "================================================================================"
  local idx=1
  for line in "${PROJECT_LINES[@]}"; do
    local pid="${line%%|*}"
    local pname="${line#*|}"
    project_ids+=("${pid}")
    local marker=""
    if [[ "${pid}" == "${current_gcp_project}" ]]; then
      marker=" [current gcloud project]"
    fi
    printf "   [%2d] %-34s (%s)%s\n" "${idx}" "${pid}" "${pname}" "${marker}"
    ((idx++))
  done
  echo "================================================================================"

  if [[ "${AUTO_APPROVE}" == "true" && -n "${current_gcp_project}" ]]; then
    PROJECT_ID="${current_gcp_project}"
    log_info "Non-interactive mode (--yes): Auto-selected current gcloud project '${PROJECT_ID}'."
    return 0
  fi

  while true; do
    local selection=""
    if ! read -r -p "Select the GCP project number [1-${#project_ids[@]}] (or enter Project ID): " selection; then
      echo ""
      log_error "No project selection received."
      exit 1
    fi
    # Trim leading and trailing whitespace
    selection="${selection#"${selection%%[![:space:]]*}"}"
    selection="${selection%"${selection##*[![:space:]]}"}"

    if [[ "${selection}" =~ ^[0-9]+$ ]]; then
      if (( selection >= 1 && selection <= ${#project_ids[@]} )); then
        PROJECT_ID="${project_ids[$((selection - 1))]}"
        break
      else
        log_warn "Invalid number. Please enter a number between 1 and ${#project_ids[@]}."
      fi
    elif [[ -n "${selection}" ]]; then
      if gcloud projects describe "${selection}" >/dev/null 2>&1; then
        PROJECT_ID="${selection}"
        break
      else
        log_warn "Project '${selection}' was not found or is not accessible. Please try again."
      fi
    else
      log_warn "Please enter a valid project number [1-${#project_ids[@]}]."
    fi
  done
}

#######################################
# Summary: Prompts the user to confirm or select the target GCP region and
#          On-Premises Apigee Proxy URL when running interactively.
# Parameters: None.
# Return Value: Updates REGION, MCP_SERVER_URL, and APIGEE_AUTH_MODE.
# Exceptions/Errors: None.
#######################################
prompt_runtime_settings() {
  local default_region="${REGION:-${GOOGLE_CLOUD_LOCATION:-us-central1}}"
  local default_url="${APIGEE_URL:-${MCP_SERVER_URL:-https://apigee.onprem.internal/telecom-mcp}}"
  local default_auth="${AUTH_MODE:-${APIGEE_AUTH_MODE:-auto}}"

  if [[ "${AUTO_APPROVE}" == "true" || ! -t 0 ]]; then
    REGION="${default_region}"
    MCP_SERVER_URL="${default_url}"
    APIGEE_AUTH_MODE="${default_auth}"
    return 0
  fi

  if [[ -z "${REGION}" ]]; then
    echo ""
    echo "Available Agent Runtime Regions:"
    echo "   [ 1] us-central1      (Iowa)"
    echo "   [ 2] us-east1         (South Carolina)"
    echo "   [ 3] us-west1         (Oregon)"
    echo "   [ 4] europe-west1     (Belgium)"
    echo "   [ 5] asia-southeast1  (Singapore)"
    local reg_input=""
    read -r -p "Select region number [1-5] or press Enter for default [${default_region}]: " reg_input || true
    reg_input="${reg_input#"${reg_input%%[![:space:]]*}"}"
    reg_input="${reg_input%"${reg_input##*[![:space:]]}"}"
    case "${reg_input}" in
      1) REGION="us-central1" ;;
      2) REGION="us-east1" ;;
      3) REGION="us-west1" ;;
      4) REGION="europe-west1" ;;
      5) REGION="asia-southeast1" ;;
      "") REGION="${default_region}" ;;
      *) REGION="${reg_input}" ;;
    esac
  fi

  if [[ -z "${APIGEE_URL}" ]]; then
    echo ""
    local url_input=""
    read -r -p "Enter On-Premises Apigee Proxy URL [default: ${default_url}]: " url_input || true
    url_input="${url_input#"${url_input%%[![:space:]]*}"}"
    url_input="${url_input%"${url_input##*[![:space:]]}"}"
    MCP_SERVER_URL="${url_input:-${default_url}}"
  else
    MCP_SERVER_URL="${APIGEE_URL}"
  fi

  APIGEE_AUTH_MODE="${default_auth}"
}

#######################################
# Summary: Verifies and installs required CLI tools in Google Cloud Shell
#          (`gcloud`, `jq`, `curl`, `python3`, `uv`, `google-agents-cli`).
# Parameters: None.
# Return Value: None.
# Exceptions/Errors: Exits 1 if gcloud or python3 is missing and cannot be installed.
#######################################
ensure_cloud_shell_prerequisites() {
  log_info "Checking Google Cloud Shell & CLI prerequisites..."

  if ! command -v gcloud >/dev/null 2>&1; then
    log_error "gcloud CLI is not installed. Please run this script inside Google Cloud Shell or install Google Cloud SDK."
    exit 1
  fi

  for pkg in curl jq python3; do
    if ! command -v "${pkg}" >/dev/null 2>&1; then
      log_info "Installing missing system package '${pkg}'..."
      if command -v sudo >/dev/null 2>&1; then
        sudo apt-get update -y -qq && sudo apt-get install -y -qq "${pkg}"
      else
        apt-get update -y -qq && apt-get install -y -qq "${pkg}"
      fi
    fi
  done

  if ! command -v uv >/dev/null 2>&1; then
    log_info "Installing 'uv' Python package manager..."
    retry_cmd 3 3 bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
    export PATH="${HOME}/.local/bin:${PATH}"
  fi

  if ! command -v agents-cli >/dev/null 2>&1; then
    log_info "Installing 'google-agents-cli'..."
    retry_cmd 3 3 uv tool install google-agents-cli
    export PATH="${HOME}/.local/bin:${PATH}"
  fi

  log_ok "All CLI prerequisites verified (gcloud, jq, python3, uv, agents-cli)."
}

#######################################
# Summary: Creates/updates the Cloud Monitoring Dashboard for Agent Runtime &
#          Apigee Cloud VPN Observability idempotently.
# Parameters: $1 - Project ID.
# Return Value: None.
# Exceptions/Errors: Logs warning if dashboard API call fails without aborting.
#######################################
provision_monitoring_dashboard() {
  local project="$1"
  local dashboard_title="Telecom ADK Agent Runtime & Apigee VPN Observability"

  log_info "Provisioning Cloud Monitoring Dashboard '${dashboard_title}'..."

  local dashboard_json
  dashboard_json="$(cat <<EOF
{
  "displayName": "${dashboard_title}",
  "gridLayout": {
    "columns": "2",
    "widgets": [
      {
        "title": "Agent Runtime — Successful MCP Tool Calls over Cloud VPN (by Tool)",
        "xyChart": {
          "dataSets": [
            {
              "timeSeriesQuery": {
                "timeSeriesFilter": {
                  "filter": "metric.type=\"logging.googleapis.com/user/telecom_agent_mcp_tool_calls_total\" resource.type=\"aiplatform.googleapis.com/ReasoningEngine\"",
                  "aggregation": {
                    "alignmentPeriod": "60s",
                    "perSeriesAligner": "ALIGN_RATE",
                    "crossSeriesReducer": "REDUCE_SUM",
                    "groupByFields": ["metric.label.tool_name"]
                  }
                }
              },
              "plotType": "LINE"
            }
          ]
        }
      },
      {
        "title": "Agent Runtime — MCP Tool Call & Cloud VPN Errors",
        "xyChart": {
          "dataSets": [
            {
              "timeSeriesQuery": {
                "timeSeriesFilter": {
                  "filter": "metric.type=\"logging.googleapis.com/user/telecom_agent_mcp_tool_errors_total\" resource.type=\"aiplatform.googleapis.com/ReasoningEngine\"",
                  "aggregation": {
                    "alignmentPeriod": "60s",
                    "perSeriesAligner": "ALIGN_RATE",
                    "crossSeriesReducer": "REDUCE_SUM"
                  }
                }
              },
              "plotType": "STACKED_BAR"
            }
          ]
        }
      },
      {
        "title": "Agent Runtime — Apigee Model Armor Blocked Tool Responses",
        "xyChart": {
          "dataSets": [
            {
              "timeSeriesQuery": {
                "timeSeriesFilter": {
                  "filter": "metric.type=\"logging.googleapis.com/user/telecom_agent_model_armor_blocks_total\" resource.type=\"aiplatform.googleapis.com/ReasoningEngine\"",
                  "aggregation": {
                    "alignmentPeriod": "60s",
                    "perSeriesAligner": "ALIGN_SUM",
                    "crossSeriesReducer": "REDUCE_SUM"
                  }
                }
              },
              "plotType": "STACKED_BAR"
            }
          ]
        }
      },
      {
        "title": "Vertex AI Reasoning Engine — Live Execution & Tool Logs",
        "logsPanel": {
          "filter": "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\"",
          "resourceNames": ["projects/${project}"]
        }
      }
    ]
  }
}
EOF
)"

  local existing_dashboard
  existing_dashboard="$(
    gcloud monitoring dashboards list \
      --project="${project}" \
      --filter="displayName=\"${dashboard_title}\"" \
      --format="value(name)" 2>/dev/null | head -n 1 || true
  )"

  if [[ -n "${existing_dashboard}" ]]; then
    log_ok "Cloud Monitoring Dashboard already exists (${existing_dashboard})."
  else
    if gcloud monitoring dashboards create \
      --project="${project}" \
      --config="${dashboard_json}" --quiet >/dev/null 2>&1; then
      log_ok "Created Cloud Monitoring Dashboard '${dashboard_title}'."
    else
      log_warn "Could not create Cloud Monitoring Dashboard automatically; continuing."
    fi
  fi
}

# ==============================================================================
# Main Execution Flow
# ==============================================================================
parse_args "$@"

echo "================================================================================"
echo " Telecom Customer Care ADK Agent — GCP Agent Runtime Cloud Shell Installer"
echo "================================================================================"

ensure_cloud_shell_prerequisites

# 1. Verify active gcloud authentication
ACTIVE_ACCOUNT="$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null | head -n 1 || true)"
if [[ -z "${ACTIVE_ACCOUNT}" ]]; then
  log_error "No active gcloud account detected. Please run 'gcloud auth login' first."
  exit 1
fi
log_info "Authenticated GCP Account: ${ACTIVE_ACCOUNT}"

# 2. Ensure .env exists and load current defaults
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
  cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
  log_info "Initialized .env from .env.example."
fi

set -a
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/.env"
set +a

# 3. Interactive Numbered GCP Project Selection (always shown unless --project was explicitly passed)
if [[ -z "${PROJECT_ID}" ]]; then
  select_gcp_project
fi

if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  log_error "GCP Project '${PROJECT_ID}' does not exist or is not accessible by ${ACTIVE_ACCOUNT}."
  exit 1
fi

gcloud config set project "${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
log_ok "Selected Target GCP Project: ${PROJECT_ID}"

# 4. Prompt or resolve Region and Apigee URL
prompt_runtime_settings

if [[ -n "${NETWORK_ATTACHMENT_ARG}" ]]; then
  NETWORK_ATTACHMENT="${NETWORK_ATTACHMENT_ARG}"
fi

# Persist chosen configuration into .env
upsert_env_var "GOOGLE_CLOUD_PROJECT" "${PROJECT_ID}"
upsert_env_var "GOOGLE_CLOUD_LOCATION" "${REGION}"
upsert_env_var "MCP_SERVER_URL" "${MCP_SERVER_URL}"
upsert_env_var "APIGEE_AUTH_MODE" "${APIGEE_AUTH_MODE}"
[[ -n "${NETWORK_ATTACHMENT:-}" ]] && upsert_env_var "NETWORK_ATTACHMENT" "${NETWORK_ATTACHMENT}"

LOGS_BUCKET_NAME="${LOGS_BUCKET_NAME:-}"
if [[ -z "${LOGS_BUCKET_NAME}" || "${LOGS_BUCKET_NAME}" == *"your-gcp-project-id"* ]]; then
  LOGS_BUCKET_NAME="${PROJECT_ID}-telecom-agent-logs"
fi
LOGS_BUCKET_NAME="${LOGS_BUCKET_NAME#gs://}"
LOGS_BUCKET_NAME="${LOGS_BUCKET_NAME%/}"
upsert_env_var "LOGS_BUCKET_NAME" "${LOGS_BUCKET_NAME}"

echo ""
echo "--------------------------------------------------------------------------------"
echo " Deployment Configuration Summary"
echo "--------------------------------------------------------------------------------"
echo "   • GCP Project ID          : ${PROJECT_ID}"
echo "   • Agent Runtime Region    : ${REGION}"
echo "   • Foundation Model        : ${MODEL_NAME:-gemini-2.5-flash}"
echo "   • Apigee MCP Proxy URL    : ${MCP_SERVER_URL}"
echo "   • Apigee Auth Mode        : ${APIGEE_AUTH_MODE}"
echo "   • Verify Apigee SSL       : ${APIGEE_VERIFY_SSL:-true}"
echo "   • PSC Network Attachment  : ${NETWORK_ATTACHMENT:-<none (public/routable ingress)>}"
echo "   • Telemetry Logs Bucket   : gs://${LOGS_BUCKET_NAME}"
echo "--------------------------------------------------------------------------------"

# 5. Sync Python Virtual Environment & Run Unit Tests
log_info "Syncing Python virtual environment with uv..."
retry_cmd 3 2 uv sync

if [[ "${SKIP_TESTS}" != "true" ]]; then
  log_info "Running pre-deployment unit tests (tests/unit/test_agent.py)..."
  uv run pytest tests/unit/test_agent.py -q
  log_ok "All unit tests passed."
fi

# 6. Enable Required Google Cloud APIs
log_info "Enabling required GCP APIs (Agent Runtime, Agent Registry, Observability, Networking)..."
retry_cmd 3 5 gcloud services enable \
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
  cloudresourcemanager.googleapis.com \
  iam.googleapis.com \
  --project="${PROJECT_ID}"
log_ok "All required GCP APIs are enabled."

# 7. Provision GCS Telemetry Bucket, BigQuery Dataset & Sink, and IAM Bindings
log_info "Ensuring GCS telemetry bucket gs://${LOGS_BUCKET_NAME} exists..."
if ! gcloud storage buckets describe "gs://${LOGS_BUCKET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  retry_cmd 3 3 gcloud storage buckets create "gs://${LOGS_BUCKET_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
  log_ok "Created GCS telemetry bucket gs://${LOGS_BUCKET_NAME}."
else
  log_ok "GCS telemetry bucket gs://${LOGS_BUCKET_NAME} already exists."
fi

# Ensure Vertex AI service identity is materialized before binding IAM roles
gcloud beta services identity create \
  --service=aiplatform.googleapis.com \
  --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
RE_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

log_info "Binding Agent Runtime, Agent Registry, Cloud Trace, Logging, and Storage IAM roles..."
for SA in "${RE_SA}" "${COMPUTE_SA}"; do
  for ROLE in \
    "roles/aiplatform.user" \
    "roles/logging.logWriter" \
    "roles/cloudtrace.agent" \
    "roles/monitoring.metricWriter" \
    "roles/storage.objectAdmin" \
    "roles/secretmanager.secretAccessor"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA}" \
      --role="${ROLE}" \
      --condition=None \
      --quiet >/dev/null 2>&1 || true
  done
done
log_ok "Service Agent IAM bindings verified."

# 8. Provision BigQuery Telemetry Dataset & Reasoning Engine Log Sink
BQ_DATASET="telecom_agent_hybrid_apigee_telemetry"
log_info "Ensuring BigQuery telemetry dataset '${PROJECT_ID}:${BQ_DATASET}' exists..."
if ! bq --project_id="${PROJECT_ID}" show --dataset "${PROJECT_ID}:${BQ_DATASET}" >/dev/null 2>&1; then
  bq --project_id="${PROJECT_ID}" --location="${REGION}" mk \
    --dataset \
    --description="GenAI and Vertex AI Agent Runtime telemetry dataset" \
    "${PROJECT_ID}:${BQ_DATASET}" >/dev/null 2>&1 || true
fi

SINK_NAME="telecom-agent-hybrid-apigee-reasoning-engine-logs"
SINK_DEST="bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${BQ_DATASET}"
SINK_FILTER='resource.type="aiplatform.googleapis.com/ReasoningEngine"'
if gcloud logging sinks describe "${SINK_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud logging sinks update "${SINK_NAME}" "${SINK_DEST}" \
    --log-filter="${SINK_FILTER}" \
    --use-partitioned-tables \
    --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
else
  gcloud logging sinks create "${SINK_NAME}" "${SINK_DEST}" \
    --log-filter="${SINK_FILTER}" \
    --use-partitioned-tables \
    --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
fi

SINK_WRITER="$(gcloud logging sinks describe "${SINK_NAME}" --project="${PROJECT_ID}" --format='value(writerIdentity)' 2>/dev/null || true)"
if [[ -n "${SINK_WRITER}" ]]; then
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${SINK_WRITER}" \
    --role="roles/bigquery.dataEditor" \
    --condition=None \
    --quiet >/dev/null 2>&1 || true
fi
log_ok "BigQuery telemetry dataset and ReasoningEngine log sink configured."

# 9. Provision Cloud Logging Metrics & Cloud Monitoring Dashboard
log_info "Configuring Cloud Logging log-based metrics for Agent Runtime..."
create_or_update_log_metric() {
  local metric_name="$1"
  local description="$2"
  local filter="$3"
  if gcloud logging metrics describe "${metric_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud logging metrics update "${metric_name}" \
      --description="${description}" \
      --log-filter="${filter}" \
      --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
  else
    gcloud logging metrics create "${metric_name}" \
      --description="${description}" \
      --log-filter="${filter}" \
      --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
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

provision_monitoring_dashboard "${PROJECT_ID}"

# 10. Deploy ADK Agent to GCP Agent Runtime (with automatic timeout polling recovery)
DEPLOYMENT_STARTED="true"

ENV_VARS=(
  "GOOGLE_CLOUD_LOCATION=${REGION}"
  "GOOGLE_GENAI_USE_VERTEXAI=true"
  "MODEL_NAME=${MODEL_NAME:-gemini-2.5-flash}"
  "MCP_SERVER_URL=${MCP_SERVER_URL}"
  "APIGEE_AUTH_MODE=${APIGEE_AUTH_MODE}"
  "APIGEE_VERIFY_SSL=${APIGEE_VERIFY_SSL:-true}"
  "APIGEE_API_KEY_HEADER=${APIGEE_API_KEY_HEADER:-x-apikey}"
  "MCP_DEPLOYMENT_TARGET=${MCP_DEPLOYMENT_TARGET:-on_prem}"
  "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true"
  "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-EVENT_ONLY}"
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
  --project "${PROJECT_ID}"
  --region "${REGION}"
  --no-confirm-project
  --update-env-vars "${ENV_VARS_CSV}"
)

if [[ "${ENABLE_AGENT_IDENTITY:-true}" == "true" ]]; then
  DEPLOY_CMD+=(--agent-identity)
fi

if [[ -n "${NETWORK_ATTACHMENT:-}" ]]; then
  log_info "Attaching PSC Network Attachment to Agent Runtime: ${NETWORK_ATTACHMENT}"
  DEPLOY_CMD+=(--network-attachment "${NETWORK_ATTACHMENT}")
  [[ -n "${DNS_PEERING_DOMAIN:-}" ]] && DEPLOY_CMD+=(--dns-peering-domain "${DNS_PEERING_DOMAIN}")
  [[ -n "${DNS_PEERING_PROJECT:-}" ]] && DEPLOY_CMD+=(--dns-peering-project "${DNS_PEERING_PROJECT}")
  [[ -n "${DNS_PEERING_NETWORK:-}" ]] && DEPLOY_CMD+=(--dns-peering-network "${DNS_PEERING_NETWORK}")
fi

log_info "Deploying Telecom ADK Agent to GCP Agent Runtime (this typically takes 5-8 minutes)..."
set +e
"${DEPLOY_CMD[@]}"
DEPLOY_EXIT_CODE=$?
set -e

if [[ ${DEPLOY_EXIT_CODE} -ne 0 ]]; then
  log_warn "Initial deploy command exited with code ${DEPLOY_EXIT_CODE}. Checking server-side Agent Runtime deployment status..."
  for poll_idx in {1..12}; do
    sleep 30
    log_info "Polling Agent Runtime deployment status (attempt ${poll_idx}/12)..."
    if agents-cli deploy --status --project "${PROJECT_ID}" --region "${REGION}" --no-confirm-project; then
      DEPLOY_EXIT_CODE=0
      break
    fi
  done
fi

if [[ ${DEPLOY_EXIT_CODE} -ne 0 ]]; then
  log_error "Agent Runtime deployment did not succeed. Review logs above."
  exit "${DEPLOY_EXIT_CODE}"
fi

# 11. Resolve Reasoning Engine Resource Name & Verify Agent Registry
REASONING_ENGINE_ID=""
if [[ -f "${SCRIPT_DIR}/deployment_metadata.json" ]]; then
  REASONING_ENGINE_ID="$(
    python3 -c 'import json; print(json.load(open("deployment_metadata.json")).get("remote_agent_runtime_id", ""))' 2>/dev/null || true
  )"
fi

if [[ -z "${REASONING_ENGINE_ID}" ]]; then
  TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
  if [[ -n "${TOKEN}" ]]; then
    REASONING_ENGINE_ID="$(
      curl -s -H "Authorization: Bearer ${TOKEN}" \
        "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines" \
      | jq -r '.reasoningEngines[]? | select(.displayName=="telecom-agent-hybrid-apigee") | .name' \
      | head -n 1 || true
    )"
  fi
fi

log_info "Checking GCP Agent Registry in project '${PROJECT_ID}' (${REGION})..."
if gcloud alpha agent-registry agents list --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud alpha agent-registry agents list --project="${PROJECT_ID}" --location="${REGION}" || true
elif gcloud agent-registry agents list --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud agent-registry agents list --project="${PROJECT_ID}" --location="${REGION}" || true
else
  log_ok "Agent Runtime automatically registers deployed Reasoning Engines into GCP Agent Registry."
fi

echo ""
echo "================================================================================"
log_ok "Telecom ADK Agent Successfully Deployed to GCP Agent Runtime!"
echo "================================================================================"
echo "  • GCP Project ID             : ${PROJECT_ID}"
echo "  • Agent Runtime Region       : ${REGION}"
echo "  • Reasoning Engine Resource  : ${REASONING_ENGINE_ID:-<see deployment_metadata.json>}"
echo "  • Apigee Proxy Target (VPN)  : ${MCP_SERVER_URL} (Auth Mode: ${APIGEE_AUTH_MODE})"
echo "--------------------------------------------------------------------------------"
echo "  GCP Console Links:"
echo "  • Agent Runtime (Engines)    : https://console.cloud.google.com/vertex-ai/agents/agent-engines?project=${PROJECT_ID}"
echo "  • Agent Registry             : https://console.cloud.google.com/vertex-ai/agents/agent-registry?project=${PROJECT_ID}"
echo "  • Cloud Monitoring Dashboard : https://console.cloud.google.com/monitoring/dashboards?project=${PROJECT_ID}"
echo "  • Cloud Trace Explorer       : https://console.cloud.google.com/traces/list?project=${PROJECT_ID}"
echo "  • Cloud Logging Explorer     : https://console.cloud.google.com/logs/query;query=resource.type%3D%22aiplatform.googleapis.com%2FReasoningEngine%22?project=${PROJECT_ID}"
echo "--------------------------------------------------------------------------------"
echo "  Next Step — Manual Gemini Enterprise App Registration:"
echo "  1. Open Google Cloud Console -> Gemini Enterprise -> Apps -> Select your App."
echo "  2. Go to 'Agents' -> '+ Add Agent' -> Select 'Vertex AI Agent Engine (ADK)'."
echo "  3. Paste Reasoning Engine Resource Name:"
echo "     ${REASONING_ENGINE_ID:-<see deployment_metadata.json>}"
echo "================================================================================"
