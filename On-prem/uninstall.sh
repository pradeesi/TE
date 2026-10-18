#!/usr/bin/env bash
# ==============================================================================
# Purpose: Fault-tolerant uninstallation script for the On-Premises Telecom
#          Customer Data Center stack on an Ubuntu VM.
# Architecture/Context:
#   - Removes ONLY the application components deployed by this project:
#       1. The 7 containers (`telecom-crm-console`, `telecom-mcp-server`,
#          `telecom-postgres`, `telecom-grafana`, `telecom-prometheus`,
#          `telecom-loki`, `telecom-portainer`) and the `telecom-net` network.
#       2. Locally built application images (`telecom-onprem/*`).
#       3. Named Docker volumes (`pg_data`, `loki_data`, `prometheus_data`,
#          `grafana_data`, `portainer_data`) unless `--keep-data` is passed.
#       4. Local Python virtualenv (`.venv`) and optionally `.env` (`--remove-env`).
#   - Deliberately leaves all OS-level prerequisites (Docker Engine, Docker
#     Compose, `containerd`, Python 3, `curl`, `jq`, `git`, etc.) untouched on
#     the Ubuntu OS so re-installing the stack later is fast and seamless.
# Dependencies/Side Effects:
#   - Destructive to the application containers and (by default) their volumes;
#     never removes OS packages or system services.
# ==============================================================================

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

CONTAINER_NAMES=(
  "telecom-crm-console"
  "telecom-mcp-server"
  "telecom-postgres"
  "telecom-grafana"
  "telecom-prometheus"
  "telecom-loki"
  "telecom-portainer"
)

IMAGE_NAMES=(
  "telecom-onprem/telecom-app:local"
  "telecom-onprem/telecom-grafana:local"
  "telecom-onprem/telecom-prometheus:local"
  "telecom-onprem/telecom-loki:local"
)

# ------------------------------------------------------------------------------
# Structured Logging Helpers
# ------------------------------------------------------------------------------
log_info() {
  printf '%s [INFO]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

log_warn() {
  printf '%s [WARN]  %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

log_error() {
  printf '%s [ERROR] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

trap 'log_error "Uninstallation encountered an unexpected error at line ${LINENO} (exit code $?)." ; exit 1' ERR

#######################################
# Summary: Prints CLI usage instructions.
# Parameters: None.
# Return Value: None.
# Exceptions/Errors: None.
#######################################
usage() {
  cat <<EOF
Usage: ./uninstall.sh [OPTIONS]

Stops and removes the On-Premises Telecom MCP, CRM, DB, Observability, and
Portainer components while leaving Ubuntu OS prerequisites (Docker, Python,
system packages) intact for future re-installation.

Options:
  --keep-data     Preserve named Docker volumes (PostgreSQL, Loki, Prometheus,
                  Grafana, and Portainer data).
  --remove-env    Also delete the local .env configuration file.
  -y, --yes       Skip interactive confirmation prompt.
  -h, --help      Show this help message and exit.
EOF
}

#######################################
# Summary: Determines whether sudo is needed to run privileged host commands.
# Parameters: None.
# Return Value: Prints 'sudo' or empty string on stdout.
# Exceptions/Errors: None.
#######################################
resolve_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    echo ""
  elif command -v sudo >/dev/null 2>&1; then
    echo "sudo"
  else
    echo ""
  fi
}

#######################################
# Summary: Safely loads environment variables from `.env` or `.env.example`,
#          falling back to default port numbers if neither file is readable.
# Parameters: None.
# Return Value: 0 always.
# Exceptions/Errors: None.
#######################################
load_env_safely() {
  set +e
  if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.env" 2>/dev/null
    set +a
  elif [[ -f "${SCRIPT_DIR}/.env.example" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.env.example" 2>/dev/null
    set +a
  fi
  set -e

  # Ensure POSTGRES_PASSWORD and GRAFANA_ADMIN_PASSWORD are set in memory so
  # `docker compose down` never fails variable interpolation if `.env` was edited.
  export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-placeholder_uninstall}"
  export GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-placeholder_uninstall}"
}

#######################################
# Summary: Fallback cleanup that forcibly removes any lingering `telecom-*`
#          containers, custom images, and volumes if `docker compose down`
#          encountered a partial state.
# Parameters: $1 - docker command ('docker' or 'sudo docker'); $2 - keep_data ('true'|'false').
# Return Value: 0 always.
# Exceptions/Errors: Logs warnings on non-fatal errors and continues.
#######################################
fallback_container_cleanup() {
  local docker_cmd="$1"
  local keep_data="$2"

  local cname
  for cname in "${CONTAINER_NAMES[@]}"; do
    if ${docker_cmd} ps -a --format '{{.Names}}' 2>/dev/null | grep -Fxq "${cname}"; then
      log_warn "Removing lingering container '${cname}'..."
      ${docker_cmd} rm -f "${cname}" >/dev/null 2>&1 || true
    fi
  done

  local img
  for img in "${IMAGE_NAMES[@]}"; do
    if ${docker_cmd} images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -Fxq "${img}"; then
      ${docker_cmd} rmi -f "${img}" >/dev/null 2>&1 || true
    fi
  done

  if [[ "${keep_data}" == "false" ]]; then
    local vol
    for vol in $({ ${docker_cmd} volume ls --format '{{.Name}}' 2>/dev/null || true; } | grep -E '(^|_)(pg_data|loki_data|prometheus_data|grafana_data|portainer_data)$' || true); do
      log_info "Removing volume '${vol}'..."
      ${docker_cmd} volume rm -f "${vol}" >/dev/null 2>&1 || true
    done
  fi
}

#######################################
# Summary: Removes UFW firewall rules opened by install.sh if UFW is active.
# Parameters: $1 - sudo prefix ('sudo' or '').
# Return Value: 0 on success.
# Exceptions/Errors: None.
#######################################
remove_firewall_rules_if_active() {
  local sudo_cmd="$1"
  if ! command -v ufw >/dev/null 2>&1; then
    return 0
  fi

  if ! ${sudo_cmd} ufw status 2>/dev/null | grep -q "Status: active"; then
    return 0
  fi

  log_info "Removing UFW firewall rules for on-premises service ports..."
  ${sudo_cmd} ufw delete allow "${CRM_PORT:-8000}/tcp" >/dev/null 2>&1 || true
  ${sudo_cmd} ufw delete allow "${MCP_PORT:-8001}/tcp" >/dev/null 2>&1 || true
  ${sudo_cmd} ufw delete allow "${GRAFANA_PORT:-3000}/tcp" >/dev/null 2>&1 || true
  ${sudo_cmd} ufw delete allow "${PROMETHEUS_PORT:-9090}/tcp" >/dev/null 2>&1 || true
  ${sudo_cmd} ufw delete allow "${LOKI_PORT:-3100}/tcp" >/dev/null 2>&1 || true
  ${sudo_cmd} ufw delete allow "${POSTGRES_PORT:-5432}/tcp" >/dev/null 2>&1 || true
  ${sudo_cmd} ufw delete allow "${PORTAINER_HTTPS_PORT:-9443}/tcp" >/dev/null 2>&1 || true
  ${sudo_cmd} ufw delete allow "${PORTAINER_HTTP_PORT:-9000}/tcp" >/dev/null 2>&1 || true
}

#######################################
# Summary: Main uninstallation workflow.
# Parameters: "$@" - CLI arguments.
# Return Value: 0 on success.
# Exceptions/Errors: Exits non-zero only on invalid CLI arguments.
#######################################
main() {
  local keep_data="false"
  local remove_env="false"
  local assume_yes="false"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --keep-data)
        keep_data="true"
        shift
        ;;
      --remove-env)
        remove_env="true"
        shift
        ;;
      -y|--yes)
        assume_yes="true"
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

  load_env_safely

  if [[ "${assume_yes}" == "false" ]]; then
    echo "This will stop and remove the Telecom On-Premises containers (MCP Server, CRM, DB, Prometheus, Loki, Grafana, Portainer)."
    echo "OS-level prerequisites (Docker Engine, Python, system packages) will remain installed."
    if [[ "${keep_data}" == "false" ]]; then
      echo "Named data volumes (PostgreSQL, Loki, Prometheus, Grafana, Portainer) will also be deleted."
    else
      echo "Named data volumes will be preserved (--keep-data)."
    fi
    read -r -p "Are you sure you want to continue? [y/N]: " reply
    if [[ ! "${reply}" =~ ^[Yy]$ ]]; then
      log_info "Uninstallation cancelled."
      exit 0
    fi
  fi

  local sudo_cmd
  sudo_cmd="$(resolve_sudo)"

  local docker_cmd="docker"
  if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    docker_cmd="${sudo_cmd} docker"
  fi

  if command -v docker >/dev/null 2>&1 && ${docker_cmd} info >/dev/null 2>&1; then
    if [[ "${keep_data}" == "true" ]]; then
      log_info "Stopping and removing Telecom containers, network, and local images (preserving volumes)..."
      if ! ${docker_cmd} compose down --rmi local --remove-orphans; then
        log_warn "docker compose down reported a warning; running fallback cleanup..."
      fi
    else
      log_info "Stopping and removing Telecom containers, network, local images, and named data volumes..."
      if ! ${docker_cmd} compose down --volumes --rmi local --remove-orphans; then
        log_warn "docker compose down reported a warning; running fallback cleanup..."
      fi
    fi

    fallback_container_cleanup "${docker_cmd}" "${keep_data}"
  else
    log_warn "Docker daemon is not running or not installed; skipping container teardown."
  fi

  if [[ -d "${SCRIPT_DIR}/.venv" ]]; then
    rm -rf "${SCRIPT_DIR}/.venv"
    log_info "Removed local Python virtual environment (.venv)."
  fi

  remove_firewall_rules_if_active "${sudo_cmd}"

  if [[ "${remove_env}" == "true" && -f "${SCRIPT_DIR}/.env" ]]; then
    rm -f "${SCRIPT_DIR}/.env"
    log_info "Removed ${SCRIPT_DIR}/.env."
  fi

  log_info "Telecom On-Premises application components removed. OS-level prerequisites were left intact."
}

main "$@"
