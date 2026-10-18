#!/usr/bin/env bash
# ==============================================================================
# Purpose: Fault-tolerant, idempotent one-command installer for the On-Premises
#          Telecom Customer Data Center stack (PostgreSQL, MCP Server, CRM
#          Application, Prometheus, Grafana Loki, Grafana, and Portainer CE)
#          on an Ubuntu VM.
# Architecture/Context:
#   - Pre-flight checks: OS compatibility, disk space, `.env` syntax/secrets,
#     and host port conflicts (ignoring existing `telecom-*` containers so
#     re-runs are 100% idempotent).
#   - OS Prerequisite Verification & Installation: Checks for and installs all
#     required Ubuntu system packages (`ca-certificates`, `curl`, `wget`,
#     `gnupg`, `lsb-release`, `jq`, `git`, `iproute2`, `netcat-openbsd`,
#     `openssl`, `python3`, `python3-pip`, `python3-venv`) as minimal Ubuntu VM
#     images may not have them pre-installed.
#   - Host Python Dependencies: Provisions a local Python virtual environment
#     (`.venv`) and installs `requirements.txt` so host-level scripts and tests
#     can also run directly on the Ubuntu VM.
#   - Docker Engine & Compose v2: Checks for and installs `docker-ce`,
#     `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, and
#     `docker-compose-plugin`, and enables `docker.service` on boot so all
#     containers (`restart: always`) automatically start on VM restart events.
#   - Health verification & failure diagnostics: Verifies all 7 containers and
#     HTTP health endpoints; on any failure, automatically dumps container status
#     and service logs (and optionally rolls back with `--rollback-on-failure`).
# Dependencies/Side Effects:
#   - Requires Ubuntu 20.04 / 22.04 / 24.04 LTS (or Debian-compatible Linux)
#     with `sudo` or root privileges.
# ==============================================================================

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

ROLLBACK_ON_FAILURE="false"
SKIP_FIREWALL="false"
DOCKER_CMD="docker"
SUDO_CMD=""

# Required Ubuntu OS packages checked and installed before Docker/application setup
REQUIRED_OS_PACKAGES=(
  "ca-certificates"
  "apt-transport-https"
  "curl"
  "wget"
  "gnupg"
  "lsb-release"
  "jq"
  "git"
  "iproute2"
  "netcat-openbsd"
  "openssl"
  "python3"
  "python3-pip"
  "python3-venv"
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

#######################################
# Summary: Global ERR trap that logs the failing line/command, dumps Docker
#          Compose diagnostics if containers were started, and optionally rolls
#          back the deployment when --rollback-on-failure is enabled.
# Parameters: $1 - Line number; $2 - Exit code.
# Return Value: Exits with the original non-zero exit code.
# Exceptions/Errors: None.
#######################################
on_error() {
  local line_no="$1"
  local exit_code="$2"
  log_error "Installation failed at line ${line_no} (exit code ${exit_code})."

  if command -v docker >/dev/null 2>&1; then
    log_error "Collecting container status and recent error logs for troubleshooting..."
    ${DOCKER_CMD} compose ps || true
    ${DOCKER_CMD} compose logs --tail=30 || true

    if [[ "${ROLLBACK_ON_FAILURE}" == "true" ]]; then
      log_warn "Rolling back partially started containers (--rollback-on-failure)..."
      ${DOCKER_CMD} compose down --remove-orphans || true
    fi
  fi

  exit "${exit_code}"
}

trap 'on_error ${LINENO} $?' ERR

#######################################
# Summary: Prints CLI usage instructions.
# Parameters: None.
# Return Value: None.
# Exceptions/Errors: None.
#######################################
usage() {
  cat <<EOF
Usage: ./install.sh [OPTIONS]

Fault-tolerant Ubuntu VM installer for the On-Premises Telecom MCP, CRM, DB,
Observability (Prometheus, Loki, Grafana), and Portainer stack.

Options:
  --skip-firewall         Do not modify UFW firewall rules even if UFW is active.
  --rollback-on-failure   Stop and remove containers if installation or health
                          checks fail.
  -h, --help              Show this help message and exit.
EOF
}

#######################################
# Summary: Executes a command with automatic retries and exponential backoff
#          to tolerate transient network blips or APT mirror timeouts.
# Parameters: $1 - Max attempts; $2 - Initial delay seconds; $3... - Command.
# Return Value: 0 if the command succeeds within max attempts; returns last
#               exit code otherwise.
# Exceptions/Errors: None.
#######################################
run_with_retry() {
  local max_attempts="$1"
  local delay_sec="$2"
  shift 2

  local attempt=1
  local rc=0
  while true; do
    if "$@"; then
      return 0
    else
      rc=$?
    fi

    if (( attempt >= max_attempts )); then
      log_error "Command failed after ${max_attempts} attempts: $*"
      return "${rc}"
    fi

    log_warn "Attempt ${attempt}/${max_attempts} failed (exit ${rc}). Retrying in ${delay_sec}s: $*"
    sleep "${delay_sec}"
    attempt=$(( attempt + 1 ))
    delay_sec=$(( delay_sec * 2 ))
  done
}

#######################################
# Summary: Determines whether sudo is needed to run privileged host commands.
# Parameters: None.
# Return Value: Prints 'sudo' or empty string on stdout.
# Exceptions/Errors: Exits with status 1 if not root and sudo is unavailable.
#######################################
resolve_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    echo ""
  elif command -v sudo >/dev/null 2>&1; then
    echo "sudo"
  else
    log_error "This script requires root privileges or 'sudo' to install packages and manage systemd."
    exit 1
  fi
}

#######################################
# Summary: Verifies OS compatibility and minimum free disk space (>= 5 GB) on
#          the Ubuntu VM before installing packages and building images.
# Parameters: None.
# Return Value: 0 on success.
# Exceptions/Errors: Exits with status 1 if disk space is critically low.
#######################################
run_preflight_checks() {
  log_info "Running Ubuntu VM pre-flight checks..."

  if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    if [[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *"ubuntu"* && "${ID_LIKE:-}" != *"debian"* ]]; then
      log_warn "Detected OS '${PRETTY_NAME:-unknown}'. This installer is designed for Ubuntu/Debian VMs."
    else
      log_info "Detected OS: ${PRETTY_NAME:-Ubuntu}."
    fi
  fi

  local avail_kb
  avail_kb="$(df -Pk "${SCRIPT_DIR}" | awk 'NR==2 {print $4}')"
  if [[ -n "${avail_kb}" ]] && (( avail_kb < 5242880 )); then
    log_error "Insufficient free disk space ($(( avail_kb / 1024 )) MB available). At least 5 GB is required for Docker images and volumes."
    exit 1
  fi
}

#######################################
# Summary: Checks each required Ubuntu OS prerequisite package and installs any
#          missing packages using apt-get with dpkg lock timeouts and retries.
# Parameters: $1 - sudo prefix ('sudo' or '').
# Return Value: 0 on success.
# Exceptions/Errors: Exits on unrecoverable apt failure.
#######################################
install_os_prerequisites() {
  local sudo_cmd="$1"
  local apt_opts=("-o" "DPkg::Lock::Timeout=120" "-o" "Acquire::Retries=3" "-y")
  local missing_pkgs=()

  log_info "Checking Ubuntu OS prerequisite packages..."
  local pkg
  for pkg in "${REQUIRED_OS_PACKAGES[@]}"; do
    if ! dpkg-query -W -f='${Status}' "${pkg}" 2>/dev/null | grep -q "install ok installed"; then
      missing_pkgs+=("${pkg}")
    fi
  done

  if (( ${#missing_pkgs[@]} == 0 )); then
    log_info "All ${#REQUIRED_OS_PACKAGES[@]} required Ubuntu OS packages are already installed."
    return 0
  fi

  log_info "Installing missing Ubuntu OS packages: ${missing_pkgs[*]}..."
  run_with_retry 3 5 ${sudo_cmd} env DEBIAN_FRONTEND=noninteractive apt-get update "${apt_opts[@]}"
  run_with_retry 3 5 ${sudo_cmd} env DEBIAN_FRONTEND=noninteractive apt-get install "${apt_opts[@]}" "${missing_pkgs[@]}"
  log_info "Ubuntu OS prerequisite packages installed successfully."
}

#######################################
# Summary: Installs Docker Engine and Docker Compose v2 plugin on Ubuntu if
#          not already installed, waiting up to 120s for dpkg/apt locks.
# Parameters: $1 - sudo prefix ('sudo' or '').
# Return Value: 0 on success.
# Exceptions/Errors: Exits on unrecoverable apt or repository failure.
#######################################
install_docker_if_needed() {
  local sudo_cmd="$1"
  local apt_opts=("-o" "DPkg::Lock::Timeout=120" "-o" "Acquire::Retries=3" "-y")

  if command -v docker >/dev/null 2>&1 && ${sudo_cmd} docker compose version >/dev/null 2>&1; then
    log_info "Docker Engine and Docker Compose v2 are already installed ($(docker --version))."
    return 0
  fi

  log_info "Configuring official Docker APT repository..."
  ${sudo_cmd} install -m 0755 -d /etc/apt/keyrings
  local tmp_gpg="/tmp/docker-archive-keyring.$$.gpg"
  rm -f "${tmp_gpg}"
  run_with_retry 3 5 curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "${tmp_gpg}"
  ${sudo_cmd} gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg "${tmp_gpg}"
  rm -f "${tmp_gpg}"
  ${sudo_cmd} chmod a+r /etc/apt/keyrings/docker.gpg

  # shellcheck disable=SC1091
  local distro_id
  distro_id="$(. /etc/os-release && echo "${ID:-ubuntu}")"
  if [[ "${distro_id}" != "ubuntu" && "${distro_id}" != "debian" ]]; then
    distro_id="ubuntu"
  fi
  # shellcheck disable=SC1091
  local codename
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-jammy}")"
  local arch
  arch="$(dpkg --print-architecture)"

  echo \
    "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${distro_id} ${codename} stable" | \
    ${sudo_cmd} tee /etc/apt/sources.list.d/docker.list >/dev/null

  log_info "Installing Docker Engine, CLI, Containerd, Buildx, and Docker Compose v2 plugin..."
  run_with_retry 3 5 ${sudo_cmd} env DEBIAN_FRONTEND=noninteractive apt-get update "${apt_opts[@]}"
  run_with_retry 3 5 ${sudo_cmd} env DEBIAN_FRONTEND=noninteractive apt-get install "${apt_opts[@]}" \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

  log_info "Docker Engine and Compose v2 installed successfully."
}

#######################################
# Summary: Creates a local Python virtual environment (`.venv`) and installs
#          Python dependencies from `requirements.txt` for host utility scripts.
# Parameters: None.
# Return Value: 0 on success.
# Exceptions/Errors: Exits if virtualenv creation or pip install fails.
#######################################
install_python_dependencies() {
  if [[ ! -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    return 0
  fi

  log_info "Checking local Python virtual environment (.venv) and application dependencies..."
  if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    python3 -m venv "${SCRIPT_DIR}/.venv"
  fi

  run_with_retry 3 5 "${SCRIPT_DIR}/.venv/bin/pip" install --quiet --upgrade pip
  run_with_retry 3 5 "${SCRIPT_DIR}/.venv/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
  log_info "Python dependencies in .venv verified and installed."
}

#######################################
# Summary: Enables containerd and docker systemd services on boot and verifies
#          that the Docker daemon is responsive.
# Parameters: $1 - sudo prefix ('sudo' or '').
# Return Value: 0 on success.
# Exceptions/Errors: Exits if the Docker daemon fails to start.
#######################################
enable_docker_on_boot() {
  local sudo_cmd="$1"
  log_info "Enabling containerd and docker systemd services to start on VM boot..."
  if [[ ! -f /etc/docker/daemon.json ]]; then
    log_info "Configuring Docker daemon log rotation defaults (/etc/docker/daemon.json: max-size=10m, max-file=3)..."
    ${sudo_cmd} mkdir -p /etc/docker
    ${sudo_cmd} tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
  fi

  if command -v systemctl >/dev/null 2>&1; then
    ${sudo_cmd} systemctl unmask containerd.service docker.service docker.socket >/dev/null 2>&1 || true
    ${sudo_cmd} systemctl enable --now containerd.service
    ${sudo_cmd} systemctl enable --now docker.service
  else
    log_warn "systemctl not found; skipping systemd boot enablement."
  fi

  local target_user="${SUDO_USER:-${USER:-}}"
  if [[ -n "${target_user}" && "${target_user}" != "root" ]]; then
    if ! id -nG "${target_user}" 2>/dev/null | grep -qw docker; then
      log_info "Adding user '${target_user}' to the 'docker' group..."
      ${sudo_cmd} usermod -aG docker "${target_user}" || true
    fi
  fi

  if docker info >/dev/null 2>&1; then
    DOCKER_CMD="docker"
  elif ${sudo_cmd} docker info >/dev/null 2>&1; then
    DOCKER_CMD="${sudo_cmd} docker"
  else
    log_error "Docker daemon is not responding even after starting docker.service."
    exit 1
  fi
}

#######################################
# Summary: Initializes `.env` from `.env.example` if missing and validates that
#          required passwords and port numbers are well-formed.
# Parameters: None.
# Return Value: 0 on success.
# Exceptions/Errors: Exits with status 1 on invalid configuration.
#######################################
initialize_env_file() {
  if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    if [[ ! -f "${SCRIPT_DIR}/.env.example" ]]; then
      log_error "Missing .env.example in ${SCRIPT_DIR}."
      exit 1
    fi
    cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
    chmod 0600 "${SCRIPT_DIR}/.env"
    log_info "Created .env from .env.example with default configuration."
  else
    log_info "Using existing .env file at ${SCRIPT_DIR}/.env."
  fi

  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
  set +a

  if [[ -z "${POSTGRES_PASSWORD:-}" || -z "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then
    log_error "POSTGRES_PASSWORD and GRAFANA_ADMIN_PASSWORD must be non-empty in .env."
    exit 1
  fi

  export PORTAINER_ADMIN_USER="${PORTAINER_ADMIN_USER:-admin}"
  export PORTAINER_ADMIN_PASSWORD="${PORTAINER_ADMIN_PASSWORD:-telecom_admin}"
  if (( ${#PORTAINER_ADMIN_PASSWORD} < 12 )); then
    log_error "PORTAINER_ADMIN_PASSWORD in .env must be at least 12 characters long (current length: ${#PORTAINER_ADMIN_PASSWORD})."
    exit 1
  fi

  local port_var
  for port_var in POSTGRES_PORT CRM_PORT MCP_PORT PROMETHEUS_PORT LOKI_PORT GRAFANA_PORT PORTAINER_HTTPS_PORT PORTAINER_HTTP_PORT; do
    local port_val="${!port_var:-}"
    if [[ -n "${port_val}" ]] && ! [[ "${port_val}" =~ ^[0-9]+$ ]]; then
      log_error "Invalid port value in .env: ${port_var}='${port_val}' (must be an integer)."
      exit 1
    fi
  done
}

#######################################
# Summary: Checks if any required host TCP port is already bound by a non-Docker
#          host process (e.g. a native PostgreSQL or Grafana service on the VM)
#          that would cause `docker compose up` to fail.
# Parameters: $1 - sudo prefix ('sudo' or '').
# Return Value: 0 if all ports are free or already owned by this stack.
# Exceptions/Errors: Exits with status 1 if a conflicting host process is found.
#######################################
check_port_conflicts() {
  local sudo_cmd="$1"
  if ! command -v ss >/dev/null 2>&1; then
    return 0
  fi

  local ports=(
    "${POSTGRES_PORT:-5432}"
    "${CRM_PORT:-8000}"
    "${MCP_PORT:-8001}"
    "${PROMETHEUS_PORT:-9090}"
    "${LOKI_PORT:-3100}"
    "${GRAFANA_PORT:-3000}"
    "${PORTAINER_HTTPS_PORT:-9443}"
    "${PORTAINER_HTTP_PORT:-9000}"
  )

  local port
  for port in "${ports[@]}"; do
    local listener
    listener="$(${sudo_cmd} ss -tulpn "sport = :${port}" 2>/dev/null | awk 'NR>1 {print $0}' || true)"
    if [[ -n "${listener}" ]] && ! echo "${listener}" | grep -Eq 'docker-proxy|containerd'; then
      log_error "Host TCP port ${port} is already in use by a non-Docker process:"
      log_error "  ${listener}"
      log_error "Stop the conflicting host service or change the port in .env before re-running ./install.sh."
      exit 1
    fi
  done
}

#######################################
# Summary: Opens the stack's TCP ports in UFW if UFW is installed and active.
# Parameters: $1 - sudo prefix ('sudo' or '').
# Return Value: 0 on success.
# Exceptions/Errors: None.
#######################################
configure_firewall_if_active() {
  local sudo_cmd="$1"
  if ! command -v ufw >/dev/null 2>&1; then
    return 0
  fi

  if ! ${sudo_cmd} ufw status 2>/dev/null | grep -q "Status: active"; then
    log_info "UFW firewall is inactive; no firewall rules needed."
    return 0
  fi

  log_info "UFW is active. Allowing on-premises service ports..."
  ${sudo_cmd} ufw allow "${CRM_PORT:-8000}/tcp" comment "Telecom CRM Application" >/dev/null || true
  ${sudo_cmd} ufw allow "${MCP_PORT:-8001}/tcp" comment "Telecom MCP Server" >/dev/null || true
  ${sudo_cmd} ufw allow "${GRAFANA_PORT:-3000}/tcp" comment "Grafana Dashboards" >/dev/null || true
  ${sudo_cmd} ufw allow "${PROMETHEUS_PORT:-9090}/tcp" comment "Prometheus Metrics" >/dev/null || true
  ${sudo_cmd} ufw allow "${LOKI_PORT:-3100}/tcp" comment "Grafana Loki Audit Logs" >/dev/null || true
  ${sudo_cmd} ufw allow "${POSTGRES_PORT:-5432}/tcp" comment "PostgreSQL DB Server" >/dev/null || true
  ${sudo_cmd} ufw allow "${PORTAINER_HTTPS_PORT:-9443}/tcp" comment "Portainer HTTPS UI" >/dev/null || true
  ${sudo_cmd} ufw allow "${PORTAINER_HTTP_PORT:-9000}/tcp" comment "Portainer HTTP UI" >/dev/null || true
  log_info "UFW firewall rules updated."
}

#######################################
# Summary: Waits for an HTTP endpoint to return HTTP 200 within a timeout; dumps
#          service logs and returns 1 on failure.
# Parameters: $1 - Service display name; $2 - Compose service name; $3 - URL;
#             $4 - Timeout in seconds.
# Return Value: 0 when healthy, 1 on timeout.
# Exceptions/Errors: None.
#######################################
wait_for_http() {
  local display_name="$1"
  local service_name="$2"
  local url="$3"
  local timeout_sec="${4:-90}"
  local elapsed=0

  log_info "Waiting for ${display_name} (${url}) to become ready..."
  while (( elapsed < timeout_sec )); do
    if curl -fsS --max-time 3 "${url}" >/dev/null 2>&1; then
      log_info "${display_name} is healthy."
      return 0
    fi
    sleep 3
    elapsed=$(( elapsed + 3 ))
  done

  log_error "${display_name} did not become healthy within ${timeout_sec}s. Recent container logs:"
  ${DOCKER_CMD} compose logs --tail=40 "${service_name}" || true
  return 1
}

#######################################
# Summary: Automatically initializes the Portainer CE administrator account via
#          `POST /api/users/admin/init` during installation so Portainer NEVER
#          triggers its 5-minute uninitialized security timeout lockout.
# Parameters: None.
# Return Value: 0 on success.
# Exceptions/Errors: Exits with status 1 if admin initialization fails.
#######################################
initialize_portainer_admin() {
  local base_url="http://127.0.0.1:${PORTAINER_HTTP_PORT:-9000}"
  local check_code
  check_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${base_url}/api/users/admin/check" || echo "000")"

  if [[ "${check_code}" == "204" ]]; then
    log_info "Portainer administrator account is already initialized."
    return 0
  fi

  log_info "Auto-initializing Portainer administrator account ('${PORTAINER_ADMIN_USER}') to prevent 5-minute security timeout..."
  local payload
  payload="$(jq -cn \
    --arg u "${PORTAINER_ADMIN_USER}" \
    --arg p "${PORTAINER_ADMIN_PASSWORD}" \
    '{Username: $u, Password: $p}')"

  local init_code
  init_code="$(curl -s -o /tmp/portainer_init.$$.json -w '%{http_code}' --max-time 10 \
    -X POST "${base_url}/api/users/admin/init" \
    -H "Content-Type: application/json" \
    --data "${payload}" || echo "000")"

  if [[ "${init_code}" != "200" ]]; then
    # If Portainer was already locked by the 5-minute timeout from a prior uninitialized run,
    # restart telecom-portainer to clear the lockout and immediately initialize the admin user.
    log_warn "Portainer returned HTTP ${init_code} on initial admin setup; restarting telecom-portainer and retrying..."
    ${DOCKER_CMD} restart telecom-portainer >/dev/null
    wait_for_http "Portainer CE" "portainer" "${base_url}/api/system/status" 60
    init_code="$(curl -s -o /tmp/portainer_init.$$.json -w '%{http_code}' --max-time 10 \
      -X POST "${base_url}/api/users/admin/init" \
      -H "Content-Type: application/json" \
      --data "${payload}" || echo "000")"
  fi

  rm -f "/tmp/portainer_init.$$.json"

  if [[ "${init_code}" == "200" || "${init_code}" == "409" ]]; then
    log_info "Portainer administrator account ('${PORTAINER_ADMIN_USER}') initialized; 5-minute timeout disabled."
    return 0
  fi

  log_error "Failed to initialize Portainer administrator account (HTTP ${init_code})."
  return 1
}

#######################################
# Summary: Main installation workflow.
# Parameters: "$@" - CLI arguments.
# Return Value: 0 on success.
# Exceptions/Errors: Triggers on_error trap on fatal errors.
#######################################
main() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --skip-firewall)
        SKIP_FIREWALL="true"
        shift
        ;;
      --rollback-on-failure)
        ROLLBACK_ON_FAILURE="true"
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

  SUDO_CMD="$(resolve_sudo)"

  run_preflight_checks
  initialize_env_file
  install_os_prerequisites "${SUDO_CMD}"
  install_python_dependencies
  install_docker_if_needed "${SUDO_CMD}"
  enable_docker_on_boot "${SUDO_CMD}"
  check_port_conflicts "${SUDO_CMD}"

  if [[ "${SKIP_FIREWALL}" == "false" ]]; then
    configure_firewall_if_active "${SUDO_CMD}"
  fi

  log_info "Validating docker-compose.yml syntax..."
  ${DOCKER_CMD} compose config -q

  log_info "Building and starting all 7 on-premises containers (restart policy: always)..."
  run_with_retry 2 5 ${DOCKER_CMD} compose up -d --build --remove-orphans

  log_info "Pruning dangling Docker build layers to keep disk usage compact..."
  ${DOCKER_CMD} image prune -f >/dev/null 2>&1 || true

  wait_for_http "MCP Server" "mcp-server" "http://127.0.0.1:${MCP_PORT:-8001}/health" 90
  wait_for_http "CRM Application" "crm-app" "http://127.0.0.1:${CRM_PORT:-8000}/health" 90
  wait_for_http "Grafana Loki" "loki" "http://127.0.0.1:${LOKI_PORT:-3100}/ready" 60
  wait_for_http "Prometheus" "prometheus" "http://127.0.0.1:${PROMETHEUS_PORT:-9090}/-/healthy" 60
  wait_for_http "Grafana" "grafana" "http://127.0.0.1:${GRAFANA_PORT:-3000}/api/health" 90
  wait_for_http "Portainer CE" "portainer" "http://127.0.0.1:${PORTAINER_HTTP_PORT:-9000}/api/system/status" 60
  initialize_portainer_admin

  local vm_ip
  vm_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  if [[ -z "${vm_ip}" ]]; then
    vm_ip="<UBUNTU_VM_IP>"
  fi

  echo ""
  echo "================================================================================"
  echo "  Telecom On-Premises Customer Data Center Stack Installed Successfully"
  echo "================================================================================"
  ${DOCKER_CMD} compose ps
  echo "--------------------------------------------------------------------------------"
  echo "  Customer Network Access URLs (Ubuntu VM IP: ${vm_ip}):"
  echo "  - Portainer CE (HTTPS) : https://${vm_ip}:${PORTAINER_HTTPS_PORT:-9443} (user: ${PORTAINER_ADMIN_USER})"
  echo "  - Portainer CE (HTTP)  : http://${vm_ip}:${PORTAINER_HTTP_PORT:-9000} (user: ${PORTAINER_ADMIN_USER})"
  echo "  - CRM Application      : http://${vm_ip}:${CRM_PORT:-8000}"
  echo "  - MCP Server (JSONRPC) : http://${vm_ip}:${MCP_PORT:-8001}/mcp"
  echo "  - MCP Server (SSE)     : http://${vm_ip}:${MCP_PORT:-8001}/sse"
  echo "  - MCP Server (Tools)   : http://${vm_ip}:${MCP_PORT:-8001}/tools"
  echo "  - Grafana Dashboards   : http://${vm_ip}:${GRAFANA_PORT:-3000} (user: ${GRAFANA_ADMIN_USER:-admin})"
  echo "  - Prometheus           : http://${vm_ip}:${PROMETHEUS_PORT:-9090}"
  echo "  - Grafana Loki         : http://${vm_ip}:${LOKI_PORT:-3100}/ready"
  echo "  - PostgreSQL DB Server : ${vm_ip}:${POSTGRES_PORT:-5432} (db: ${POSTGRES_DB:-telecom_db})"
  echo "================================================================================"
}

main "$@"
