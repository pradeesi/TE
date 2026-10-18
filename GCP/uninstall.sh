#!/usr/bin/env bash
# ==============================================================================
# Author: Pradeep Singh
# License: Apache-2.0
#
# Purpose:
#   Fault-tolerant, interactive Google Cloud Shell uninstallation script for the
#   Telecom Operations ADK Agent (`telecom-agent-hybrid-apigee`). Safely removes
#   ONLY the resources created for this agent in the selected GCP project.
#
# Architecture/Context:
#   Executes directly from Google Cloud Shell or any Linux workstation inside
#   `TE/GCP/`. Prompts the operator with an interactive numbered list of
#   accessible GCP projects, confirms teardown, and cleanly removes:
#     1. Vertex AI Agent Engine (`ReasoningEngine` instance for
#        `telecom-agent-hybrid-apigee`) and any custom GCP Agent Registry entry
#     2. Cloud Monitoring Dashboard (`Telecom ADK Agent Runtime & Apigee VPN Observability`)
#     3. Cloud Logging log-based metrics (`telecom_agent_mcp_tool_calls_total`,
#        `telecom_agent_mcp_tool_errors_total`, `telecom_agent_model_armor_blocks_total`)
#     4. Cloud Logging BigQuery sink (`telecom-agent-hybrid-apigee-reasoning-engine-logs`)
#     5. BigQuery telemetry dataset (`telecom_agent_hybrid_apigee_telemetry`) and
#        GCS prompt/response bucket (`gs://<PROJECT_ID>-telecom-agent-logs`),
#        unless `--keep-data` is specified
#     6. Local deployment metadata (`deployment_metadata.json`) and optional
#        `.venv` / `.env` files
#
# Dependencies/Side Effects:
#   Requires `gcloud`, `curl`, and `jq`. Does not delete unrelated Vertex AI
#   Reasoning Engines, buckets, or datasets in the target GCP project.
# ==============================================================================

set -Eeuo pipefail

# ------------------------------------------------------------------------------
# Terminal Formatting & Structured Logging
# ------------------------------------------------------------------------------
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly CYAN='\033[0;36m'
readonly BOLD='\033[1m'
readonly NC='\033[0m'

log_info()  { printf "${BLUE}[INFO]${NC}  [%s] %s\n" "$(date +'%Y-%m-%d %H:%M:%S')" "$*"; }
log_ok()    { printf "${GREEN}[OK]${NC}    [%s] %s\n" "$(date +'%Y-%m-%d %H:%M:%S')" "$*"; }
log_warn()  { printf "${YELLOW}[WARN]${NC}  [%s] %s\n" "$(date +'%Y-%m-%d %H:%M:%S')" "$*" >&2; }
log_error() { printf "${RED}[ERROR]${NC} [%s] %s\n" "$(date +'%Y-%m-%d %H:%M:%S')" "$*" >&2; }
log_step()  { printf "\n${CYAN}${BOLD}==> %s${NC}\n" "$*"; }

# ------------------------------------------------------------------------------
# Global Error Trap
# ------------------------------------------------------------------------------
on_error() {
  local exit_code=$?
  local line_no=${1:-unknown}
  log_error "Uninstallation encountered an unexpected error at line ${line_no} (exit code: ${exit_code})."
  log_warn "You can safely re-run ./uninstall.sh — all teardown steps are idempotent."
  exit "${exit_code}"
}
trap 'on_error $LINENO' ERR

# ------------------------------------------------------------------------------
# Path & Default Configuration
# ------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CLI_PROJECT_ID=""
CLI_REGION=""
NON_INTERACTIVE="false"
KEEP_DATA="false"
CLEAN_VENV="false"
REMOVE_ENV="false"

# ------------------------------------------------------------------------------
# Function: usage
# Summary: Prints CLI options and usage examples for Cloud Shell operators.
# Parameters: None
# Returns: 0
# ------------------------------------------------------------------------------
usage() {
  printf "%b" "${BOLD}Telecom ADK Agent — GCP Cloud Shell Uninstaller${NC}

${BOLD}USAGE:${NC}
  ./uninstall.sh [OPTIONS]

${BOLD}OPTIONS:${NC}
  -p, --project <PROJECT_ID>       Target GCP Project ID (skips numbered project menu)
  -r, --region <REGION>            GCP Region (default: read from deployment_metadata.json/.env or us-central1)
  -y, --yes                        Non-interactive mode (skips confirmation prompt)
      --keep-data                  Retain the GCS telemetry bucket and BigQuery dataset
      --clean-venv                 Also remove the local Python .venv directory
      --remove-env                 Also remove the local .env configuration file
  -h, --help                       Display this help message and exit

${BOLD}EXAMPLES:${NC}
  # Interactive uninstallation (presents numbered list of GCP projects):
  ./uninstall.sh

  # Non-interactive uninstallation for a specific project:
  ./uninstall.sh --project my-gcp-project-id --region us-central1 --yes

  # Uninstall runtime & dashboards while keeping historical logs in GCS/BigQuery:
  ./uninstall.sh --keep-data
"
}

# ------------------------------------------------------------------------------
# Function: parse_args
# Summary: Parses command-line arguments and flags.
# Parameters: "$@" - Command-line arguments
# Returns: 0 on valid args, exits 1 on invalid flag.
# ------------------------------------------------------------------------------
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -p|--project)
        [[ -n "${2:-}" ]] || { log_error "Missing value for $1"; exit 1; }
        CLI_PROJECT_ID="$2"
        shift 2
        ;;
      -r|--region)
        [[ -n "${2:-}" ]] || { log_error "Missing value for $1"; exit 1; }
        CLI_REGION="$2"
        shift 2
        ;;
      -y|--yes)
        NON_INTERACTIVE="true"
        shift
        ;;
      --keep-data)
        KEEP_DATA="true"
        shift
        ;;
      --clean-venv)
        CLEAN_VENV="true"
        shift
        ;;
      --remove-env)
        REMOVE_ENV="true"
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

# ------------------------------------------------------------------------------
# Function: check_gcloud_auth
# Summary: Ensures gcloud is installed and authenticated.
# Parameters: None
# Returns: 0 on active session, exits 1 otherwise.
# ------------------------------------------------------------------------------
check_gcloud_auth() {
  log_step "Step 1/7: Verifying Google Cloud Authentication"

  if ! command -v gcloud >/dev/null 2>&1; then
    log_error "gcloud CLI is not installed."
    exit 1
  fi

  local active_account
  active_account="$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null | head -n 1 || true)"

  if [[ -z "${active_account}" ]]; then
    log_warn "No active gcloud account found. Launching 'gcloud auth login'..."
    gcloud auth login --quiet
    active_account="$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null | head -n 1 || true)"
  fi

  if [[ -z "${active_account}" ]]; then
    log_error "Authentication failed. Run 'gcloud auth login' and try again."
    exit 1
  fi

  log_ok "Authenticated as: ${BOLD}${active_account}${NC}"
}

# ------------------------------------------------------------------------------
# Function: select_gcp_project
# Summary: Lists accessible GCP projects as a numbered menu and prompts the
#          user to select one by number (or accepts --project / manual input).
# Parameters: None
# Returns: Sets global SELECTED_PROJECT_ID.
# ------------------------------------------------------------------------------
select_gcp_project() {
  log_step "Step 2/7: Selecting Target Google Cloud Project for Uninstallation"

  # Load existing .env if present for default hints
  if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.env"
    set +a
  fi

  # Discover project from deployment_metadata.json if present
  local metadata_project=""
  if [[ -f "${SCRIPT_DIR}/deployment_metadata.json" ]] && command -v jq >/dev/null 2>&1; then
    local runtime_id
    runtime_id="$(jq -r '.remote_agent_runtime_id // empty' "${SCRIPT_DIR}/deployment_metadata.json" 2>/dev/null || true)"
    if [[ "${runtime_id}" =~ ^projects/([^/]+)/locations/([^/]+)/reasoningEngines/([^/]+)$ ]]; then
      metadata_project="${BASH_REMATCH[1]}"
      METADATA_REGION="${BASH_REMATCH[2]}"
      METADATA_ENGINE_ID="${BASH_REMATCH[3]}"
    fi
  fi

  if [[ -n "${CLI_PROJECT_ID}" ]]; then
    SELECTED_PROJECT_ID="${CLI_PROJECT_ID}"
    log_info "Using GCP project from CLI flag: ${BOLD}${SELECTED_PROJECT_ID}${NC}"
  elif [[ "${NON_INTERACTIVE}" == "true" ]]; then
    local current_cfg_proj
    current_cfg_proj="$(gcloud config get-value project 2>/dev/null || true)"
    SELECTED_PROJECT_ID="${metadata_project:-${GOOGLE_CLOUD_PROJECT:-${current_cfg_proj}}}"
    if [[ -z "${SELECTED_PROJECT_ID}" || "${SELECTED_PROJECT_ID}" == "(unset)" || "${SELECTED_PROJECT_ID}" == "your-gcp-project-id" ]]; then
      log_error "Non-interactive mode requires --project <PROJECT_ID> or a configured project."
      exit 1
    fi
    log_info "Non-interactive mode: using GCP project ${BOLD}${SELECTED_PROJECT_ID}${NC}"
  else
    log_info "Fetching accessible GCP projects for your account..."
    local current_gcloud_proj
    current_gcloud_proj="$(gcloud config get-value project 2>/dev/null || true)"
    [[ "${current_gcloud_proj}" == "(unset)" ]] && current_gcloud_proj=""

    local default_hint="${metadata_project:-${GOOGLE_CLOUD_PROJECT:-${current_gcloud_proj}}}"
    [[ "${default_hint}" == "your-gcp-project-id" ]] && default_hint="${current_gcloud_proj}"

    mapfile -t project_lines < <(
      gcloud projects list \
        --filter="lifecycleState:ACTIVE" \
        --sort-by="projectId" \
        --format="value[separator='|'](projectId,name)" 2>/dev/null || true
    )

    if [[ ${#project_lines[@]} -eq 0 ]]; then
      log_warn "Could not enumerate GCP projects automatically."
      read -r -p "Enter target GCP Project ID to uninstall from${default_hint:+ [${default_hint}]}: " manual_proj
      SELECTED_PROJECT_ID="${manual_proj:-${default_hint}}"
    else
      printf "\n${BOLD}Available Google Cloud Projects:${NC}\n"
      printf -- "------------------------------------------------------------------------\n"
      local idx=1
      local default_idx=""
      declare -a proj_ids=()

      for entry in "${project_lines[@]}"; do
        local pid="${entry%%|*}"
        local pname="${entry#*|}"
        proj_ids+=("${pid}")

        local marker=""
        if [[ -n "${default_hint}" && "${pid}" == "${default_hint}" ]]; then
          marker=" ${GREEN}(deployed / active project)${NC}"
          default_idx="${idx}"
        fi

        printf "  ${BOLD}[%2d]${NC} %-34s %s%b\n" "${idx}" "${pid}" "${pname}" "${marker}"
        ((idx++))
      done
      printf "  ${BOLD}[ 0]${NC} Enter a different GCP Project ID manually\n"
      printf -- "------------------------------------------------------------------------\n"

      while true; do
        local prompt_msg="Select GCP project number [1-${#proj_ids[@]}]"
        if [[ -n "${default_idx}" ]]; then
          prompt_msg+=" (Press Enter for [${default_idx}] ${default_hint})"
        fi
        read -r -p "${prompt_msg}: " selection

        if [[ -z "${selection}" && -n "${default_idx}" ]]; then
          selection="${default_idx}"
        fi

        if [[ "${selection}" == "0" ]]; then
          read -r -p "Enter GCP Project ID: " manual_proj
          if [[ -n "${manual_proj}" ]]; then
            SELECTED_PROJECT_ID="${manual_proj}"
            break
          fi
        elif [[ "${selection}" =~ ^[0-9]+$ ]] && (( selection >= 1 && selection <= ${#proj_ids[@]} )); then
          SELECTED_PROJECT_ID="${proj_ids[$((selection - 1))]}"
          break
        elif [[ "${selection}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
          SELECTED_PROJECT_ID="${selection}"
          break
        fi

        log_warn "Invalid selection '${selection}'. Please enter a number between 1 and ${#proj_ids[@]} (or 0 for manual entry)."
      done
    fi
  fi

  if [[ -z "${SELECTED_PROJECT_ID:-}" ]]; then
    log_error "No GCP Project ID selected. Aborting."
    exit 1
  fi

  gcloud config set project "${SELECTED_PROJECT_ID}" >/dev/null 2>&1 || true
  export GOOGLE_CLOUD_PROJECT="${SELECTED_PROJECT_ID}"
  log_ok "Selected GCP Project: ${BOLD}${SELECTED_PROJECT_ID}${NC}"

  # Resolve region and agent metadata
  SELECTED_REGION="${CLI_REGION:-${METADATA_REGION:-${GOOGLE_CLOUD_LOCATION:-us-central1}}}"
  export GOOGLE_CLOUD_LOCATION="${SELECTED_REGION}"
  export AGENT_NAME="${AGENT_NAME:-telecom-agent-hybrid-apigee}"
  export LOGS_BUCKET_NAME="${LOGS_BUCKET_NAME:-${SELECTED_PROJECT_ID}-telecom-agent-logs}"
  export BQ_TELEMETRY_DATASET="${BQ_TELEMETRY_DATASET:-telecom_agent_hybrid_apigee_telemetry}"

  # Confirm uninstallation unless --yes is set
  if [[ "${NON_INTERACTIVE}" != "true" ]]; then
    printf "\n${YELLOW}${BOLD}WARNING: You are about to uninstall the Telecom ADK Agent from:${NC}\n"
    printf "  • GCP Project:        ${BOLD}%s${NC}\n" "${SELECTED_PROJECT_ID}"
    printf "  • GCP Region:         ${BOLD}%s${NC}\n" "${SELECTED_REGION}"
    printf "  • Agent Name:         ${BOLD}%s${NC}\n" "${AGENT_NAME}"
    printf "  • Keep Telemetry Data:${BOLD} %s${NC}\n\n" "${KEEP_DATA}"

    read -r -p "Are you sure you want to proceed with uninstallation? [y/N]: " confirm
    if [[ ! "${confirm}" =~ ^[Yy]([Ee][Ss])?$ ]]; then
      log_info "Uninstallation cancelled by user."
      exit 0
    fi
  fi
}

# ------------------------------------------------------------------------------
# Function: remove_reasoning_engine_and_registry
# Summary: Deletes the Vertex AI ReasoningEngine resource for
#          `telecom-agent-hybrid-apigee` and any custom Agent Registry entries.
# Parameters: None
# Returns: 0
# ------------------------------------------------------------------------------
remove_reasoning_engine_and_registry() {
  log_step "Step 3/7: Removing Vertex AI Agent Runtime (ReasoningEngine) & Agent Registry Entry"

  local token
  token="$(gcloud auth print-access-token 2>/dev/null || true)"
  if [[ -z "${token}" ]]; then
    log_warn "Unable to obtain gcloud access token; skipping REST API cleanup."
    return 0
  fi

  declare -A engine_names_map=()

  # 1. Check deployment_metadata.json first
  if [[ -f "${SCRIPT_DIR}/deployment_metadata.json" ]] && command -v jq >/dev/null 2>&1; then
    local meta_id
    meta_id="$(jq -r '.remote_agent_runtime_id // empty' "${SCRIPT_DIR}/deployment_metadata.json" 2>/dev/null || true)"
    if [[ "${meta_id}" =~ ^projects/${SELECTED_PROJECT_ID}/locations/[^/]+/reasoningEngines/[^/]+$ ]]; then
      engine_names_map["${meta_id}"]=1
    fi
  fi

  # 2. Discover any ReasoningEngine instances matching AGENT_NAME in the region
  local list_url="https://${SELECTED_REGION}-aiplatform.googleapis.com/v1beta1/projects/${SELECTED_PROJECT_ID}/locations/${SELECTED_REGION}/reasoningEngines"
  local list_resp
  list_resp="$(curl -sS -H "Authorization: Bearer ${token}" "${list_url}" 2>/dev/null || echo "{}")"

  if command -v jq >/dev/null 2>&1; then
    while IFS= read -r discovered_name; do
      [[ -n "${discovered_name}" ]] && engine_names_map["${discovered_name}"]=1
    done < <(
      echo "${list_resp}" | jq -r --arg name "${AGENT_NAME}" '
        .reasoningEngines[]?
        | select(.displayName == $name or (.displayName | tostring | test("telecom-agent-hybrid-apigee"; "i")))
        | .name
      ' 2>/dev/null || true
    )
  fi

  if [[ ${#engine_names_map[@]} -eq 0 ]]; then
    log_info "No Vertex AI ReasoningEngine instances found for '${AGENT_NAME}' in ${SELECTED_PROJECT_ID} (${SELECTED_REGION})."
  else
    for engine_resource in "${!engine_names_map[@]}"; do
      local engine_region="${SELECTED_REGION}"
      if [[ "${engine_resource}" =~ ^projects/[^/]+/locations/([^/]+)/reasoningEngines/ ]]; then
        engine_region="${BASH_REMATCH[1]}"
      fi
      local delete_url="https://${engine_region}-aiplatform.googleapis.com/v1beta1/${engine_resource}?force=true"
      log_info "Deleting Vertex AI ReasoningEngine: ${BOLD}${engine_resource}${NC}"

      local del_resp
      del_resp="$(curl -sS -X DELETE -H "Authorization: Bearer ${token}" "${delete_url}" 2>/dev/null || echo "{}")"
      if echo "${del_resp}" | grep -q '"error"'; then
        local err_code
        err_code="$(echo "${del_resp}" | jq -r '.error.code // empty' 2>/dev/null || true)"
        if [[ "${err_code}" == "404" ]]; then
          log_info "ReasoningEngine ${engine_resource} was already deleted."
        else
          log_warn "Vertex AI delete response for ${engine_resource}: ${del_resp}"
        fi
      else
        log_ok "Deleted Vertex AI ReasoningEngine: ${engine_resource}"
      fi
    done
  fi

  # 3. Clean up any explicit custom registration in GCP Agent Registry
  local reg_url="https://agentregistry.googleapis.com/v1alpha/projects/${SELECTED_PROJECT_ID}/locations/${SELECTED_REGION}/services"
  local reg_resp
  reg_resp="$(curl -sS -H "Authorization: Bearer ${token}" -H "x-goog-user-project: ${SELECTED_PROJECT_ID}" "${reg_url}" 2>/dev/null || echo "{}")"

  if command -v jq >/dev/null 2>&1; then
    while IFS= read -r svc_name; do
      [[ -z "${svc_name}" ]] && continue
      log_info "Removing custom Agent Registry service entry: ${svc_name}"
      curl -sS -X DELETE \
        -H "Authorization: Bearer ${token}" \
        -H "x-goog-user-project: ${SELECTED_PROJECT_ID}" \
        "https://agentregistry.googleapis.com/v1alpha/${svc_name}" >/dev/null 2>&1 || true
      log_ok "Removed Agent Registry service: ${svc_name}"
    done < <(
      echo "${reg_resp}" | jq -r --arg name "${AGENT_NAME}" '
        .services[]?
        | select(.displayName == $name or (.name | tostring | test($name; "i")))
        | .name
      ' 2>/dev/null || true
    )
  fi
}

# ------------------------------------------------------------------------------
# Function: remove_monitoring_dashboard
# Summary: Deletes the Cloud Monitoring dashboard created for the Telecom Agent.
# Parameters: None
# Returns: 0
# ------------------------------------------------------------------------------
remove_monitoring_dashboard() {
  log_step "Step 4/7: Removing Cloud Monitoring Dashboard"

  local dashboard_title="Telecom ADK Agent Runtime & Apigee VPN Observability"
  local dashboards_found=0

  if command -v jq >/dev/null 2>&1; then
    while IFS= read -r dash_id; do
      [[ -z "${dash_id}" ]] && continue
      dashboards_found=1
      log_info "Deleting Cloud Monitoring Dashboard: ${dash_id}"
      gcloud monitoring dashboards delete "${dash_id}" \
        --project="${SELECTED_PROJECT_ID}" \
        --quiet >/dev/null 2>&1 || true
      log_ok "Deleted Cloud Monitoring Dashboard (${dash_id})"
    done < <(
      gcloud monitoring dashboards list \
        --project="${SELECTED_PROJECT_ID}" \
        --format="json" 2>/dev/null \
        | jq -r --arg title "${dashboard_title}" '.[] | select(.displayName == $title) | .name' 2>/dev/null || true
    )
  fi

  if [[ "${dashboards_found}" -eq 0 ]]; then
    log_info "No Cloud Monitoring Dashboard named '${dashboard_title}' found."
  fi
}

# ------------------------------------------------------------------------------
# Function: remove_logging_metrics_and_sink
# Summary: Removes the 3 log-based metrics and the BigQuery log sink.
# Parameters: None
# Returns: 0
# ------------------------------------------------------------------------------
remove_logging_metrics_and_sink() {
  log_step "Step 5/7: Removing Cloud Logging Metrics & BigQuery Log Sink"

  local metrics=(
    "telecom_agent_mcp_tool_calls_total"
    "telecom_agent_mcp_tool_errors_total"
    "telecom_agent_model_armor_blocks_total"
  )

  for metric_name in "${metrics[@]}"; do
    if gcloud logging metrics describe "${metric_name}" --project="${SELECTED_PROJECT_ID}" >/dev/null 2>&1; then
      gcloud logging metrics delete "${metric_name}" \
        --project="${SELECTED_PROJECT_ID}" \
        --quiet >/dev/null 2>&1 || true
      log_ok "Deleted log-based metric: ${metric_name}"
    else
      log_info "Log-based metric '${metric_name}' not present (already removed)."
    fi
  done

  local sink_name="${AGENT_NAME}-reasoning-engine-logs"
  if gcloud logging sinks describe "${sink_name}" --project="${SELECTED_PROJECT_ID}" >/dev/null 2>&1; then
    gcloud logging sinks delete "${sink_name}" \
      --project="${SELECTED_PROJECT_ID}" \
      --quiet >/dev/null 2>&1 || true
    log_ok "Deleted Cloud Logging sink: ${sink_name}"
  else
    log_info "Cloud Logging sink '${sink_name}' not present (already removed)."
  fi
}

# ------------------------------------------------------------------------------
# Function: remove_telemetry_storage
# Summary: Deletes the BigQuery dataset and GCS telemetry bucket unless
#          --keep-data was passed.
# Parameters: None
# Returns: 0
# ------------------------------------------------------------------------------
remove_telemetry_storage() {
  log_step "Step 6/7: Cleaning Up Telemetry Storage (BigQuery & Cloud Storage)"

  if [[ "${KEEP_DATA}" == "true" ]]; then
    log_info "--keep-data specified: retaining BigQuery dataset (${BQ_TELEMETRY_DATASET}) and GCS bucket (gs://${LOGS_BUCKET_NAME})."
    return 0
  fi

  if command -v bq >/dev/null 2>&1; then
    if bq --project_id="${SELECTED_PROJECT_ID}" show "${SELECTED_PROJECT_ID}:${BQ_TELEMETRY_DATASET}" >/dev/null 2>&1; then
      bq --project_id="${SELECTED_PROJECT_ID}" rm -r -f -d "${SELECTED_PROJECT_ID}:${BQ_TELEMETRY_DATASET}" >/dev/null 2>&1 || true
      log_ok "Deleted BigQuery dataset: ${SELECTED_PROJECT_ID}:${BQ_TELEMETRY_DATASET}"
    else
      log_info "BigQuery dataset '${BQ_TELEMETRY_DATASET}' not present."
    fi
  fi

  if gcloud storage buckets describe "gs://${LOGS_BUCKET_NAME}" --project="${SELECTED_PROJECT_ID}" >/dev/null 2>&1; then
    log_info "Deleting GCS telemetry bucket gs://${LOGS_BUCKET_NAME} and its objects..."
    gcloud storage rm --recursive "gs://${LOGS_BUCKET_NAME}" \
      --project="${SELECTED_PROJECT_ID}" \
      --quiet >/dev/null 2>&1 || true
    log_ok "Deleted GCS bucket: gs://${LOGS_BUCKET_NAME}"
  else
    log_info "GCS bucket 'gs://${LOGS_BUCKET_NAME}' not present."
  fi
}

# ------------------------------------------------------------------------------
# Function: clean_local_artifacts
# Summary: Removes local deployment_metadata.json and optional .venv / .env.
# Parameters: None
# Returns: 0
# ------------------------------------------------------------------------------
clean_local_artifacts() {
  log_step "Step 7/7: Cleaning Up Local Metadata Artifacts"

  if [[ -f "${SCRIPT_DIR}/deployment_metadata.json" ]]; then
    rm -f "${SCRIPT_DIR}/deployment_metadata.json"
    log_ok "Removed local deployment_metadata.json"
  fi

  if [[ "${CLEAN_VENV}" == "true" && -d "${SCRIPT_DIR}/.venv" ]]; then
    rm -rf "${SCRIPT_DIR}/.venv"
    log_ok "Removed local Python virtual environment (.venv)"
  fi

  if [[ "${REMOVE_ENV}" == "true" && -f "${SCRIPT_DIR}/.env" ]]; then
    rm -f "${SCRIPT_DIR}/.env"
    log_ok "Removed local .env file"
  fi

  printf "\n${GREEN}${BOLD}==============================================================================${NC}\n"
  printf "${GREEN}${BOLD}  TELECOM ADK AGENT UNINSTALLATION COMPLETE${NC}\n"
  printf "${GREEN}${BOLD}==============================================================================${NC}\n"
  printf "  ${BOLD}GCP Project:${NC}          %s\n" "${SELECTED_PROJECT_ID}"
  printf "  ${BOLD}GCP Region:${NC}           %s\n" "${SELECTED_REGION}"
  printf "  ${BOLD}Agent Runtime:${NC}        Removed (%s)\n" "${AGENT_NAME}"
  printf "  ${BOLD}Monitoring & Sinks:${NC}   Removed\n"
  if [[ "${KEEP_DATA}" == "true" ]]; then
    printf "  ${BOLD}Telemetry Storage:${NC}    Retained (--keep-data)\n"
  else
    printf "  ${BOLD}Telemetry Storage:${NC}    Removed (GCS & BigQuery)\n"
  fi
  printf "${GREEN}${BOLD}==============================================================================${NC}\n"
}

# ------------------------------------------------------------------------------
# Main Execution Flow
# ------------------------------------------------------------------------------
main() {
  parse_args "$@"
  check_gcloud_auth
  select_gcp_project
  remove_reasoning_engine_and_registry
  remove_monitoring_dashboard
  remove_logging_metrics_and_sink
  remove_telemetry_storage
  clean_local_artifacts
}

main "$@"
