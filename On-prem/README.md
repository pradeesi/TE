# Telecom On-Premises Customer Data Center Stack (MCP Server, CRM, DB & Observability)

This repository contains the on-premises **Customer Data Center** components for the Telecom Agent-to-MCP architecture, packaged for deployment on a single **Ubuntu VM** using Docker Compose and managed with **Portainer CE**.

> **Scope Note**: The **Apigee Hybrid** runtime is installed and managed separately by the customer on a dedicated set of VMs. Apigee Hybrid routes northbound AI Agent requests across the customer network to the **MCP Server** (`http://<UBUNTU_VM_IP>:8001/mcp`) running on this Ubuntu VM.

---

## Application Architecture

```
+---------------------------------------------------------------------------------------------------+
|                               Customer Data Center (On-Premises)                                  |
|                                                                                                   |
|  +----------------------------+           +----------------------------------------------------+  |
|  | Separate Customer VMs      |           | Ubuntu VM (This Deployment - Docker Compose)       |  |
|  |                            |           |                                                    |  |
|  |  +----------------------+  |  (4),(8)  |  +--------------------+     +-------------------+  |  |
|  |  |    Apigee Hybrid     |==+===========+=>|     MCP Server     |====>|     DB Server     |  |  |
|  |  | (Managed Separately) |  | :8001/mcp |  |  (FastAPI/JSONRPC) |(5,6)|  (PostgreSQL 16)  |  |  |
|  |  +----------------------+  |           |  |     Port: 8001     |<====|    Port: 5432     |  |  |
|  +----------------------------+           |  +---------+----------+     +---------+---------+  |  |
|                                           |            |                          ^            |  |
|                                           |            | (7) Audit & Metrics      |            |  |
|                                           |            v                          |            |  |
|                                           |  +--------------------+     +---------+---------+  |  |
|                                           |  |   Observability    |     |  CRM Application  |  |  |
|                                           |  |                    |     |  (Agent Console)  |  |  |
|                                           |  | - Prometheus :9090 |     |    Port: 8000     |  |  |
|                                           |  | - Loki       :3100 |     +-------------------+  |  |
|                                           |  | - Grafana    :3000 |                            |  |
|                                           |  +--------------------+     +-------------------+  |  |
|                                           |                             |   Portainer CE    |  |  |
|                                           |                             | (Container Admin) |  |  |
|                                           |                             | Ports: 9443, 9000 |  |  |
|                                           |                             +-------------------+  |  |
|                                           +----------------------------------------------------+  |
+---------------------------------------------------------------------------------------------------+
```

### Component Breakdown

| Component | Container Name | Port(s) | Responsibility |
| :--- | :--- | :--- | :--- |
| **CRM Application** | `telecom-crm-console` | `8000` | Web-based Customer Care Agent Console displaying Subscriber 360 dossiers, network outages, billing history, and call logs. |
| **MCP Server** | `telecom-mcp-server` | `8001` | Model Context Protocol (MCP) server (JSON-RPC 2.0 over HTTP & SSE) exposing 8 telecom customer-care tools and returning raw subscriber records to Apigee Hybrid. |
| **DB Server** | `telecom-postgres` | `5432` | PostgreSQL 16 relational database shared by the CRM Application and MCP Server, auto-seeded on startup with 5 subscriber personas, devices, and 6 months of billing history. |
| **Prometheus** | `telecom-prometheus` | `9090` | Time-series telemetry server scraping MCP tool invocation counters and latency histograms (`mcp-server:8001/metrics`) every 10 seconds. |
| **Grafana Loki** | `telecom-loki` | `3100` | Restricted audit log store receiving full JSON-RPC request/response payloads from the MCP Server (`http://loki:3100/loki/api/v1/push`). |
| **Grafana** | `telecom-grafana` | `3000` | Observability UI pre-provisioned with Prometheus and Loki datasources plus the **Telecom MCP Security** and **AI Agent Observability** dashboards. |
| **Portainer CE** | `telecom-portainer` | `9443` (HTTPS)<br>`9000` (HTTP) | Container management UI attached to `/var/run/docker.sock` to monitor container health, inspect logs, view resource utilization, and restart services. |

---

## Project Directory Structure

```text
TE/On-prem/
├── .dockerignore                         # Excludes .env, virtualenvs, and tests from Docker builds
├── .env.example                          # Template environment configuration for the Ubuntu VM
├── .gitignore                            # Git exclusion rules for secrets and runtime artifacts
├── APIGEE_CONFIGURATION_GUIDE.md         # Step-by-step Apigee Hybrid / Apigee X proxy configuration guide
├── Dockerfile                            # Shared Python 3.11 image for MCP Server and CRM Application
├── README.md                             # Ubuntu VM installation, network access, and Postman guide
├── docker-compose.yml                    # 7-container stack with restart: always and persistent volumes
├── install.sh                            # Automated one-command Ubuntu VM installer script
├── uninstall.sh                          # Automated one-command Ubuntu VM uninstaller script
├── pyproject.toml                        # Python package metadata and test configuration
├── requirements.txt                      # Pinned Python dependencies
├── deploy/                               # Observability container definitions & configurations
│   ├── grafana/
│   │   ├── Dockerfile                    # Grafana 10.4.0 image with preloaded dashboards
│   │   ├── entrypoint.sh                 # Validates admin credentials and launches Grafana
│   │   ├── dashboards/
│   │   │   ├── ai_agent_observability.json
│   │   │   └── telecom_mcp_security.json
│   │   └── provisioning/
│   │       ├── dashboards/dashboards.yml
│   │       └── datasources/datasources.yml
│   ├── loki/
│   │   ├── Dockerfile                    # Grafana Loki 3.0.0 audit log store image
│   │   ├── entrypoint.sh                 # Starts Loki with persistent filesystem storage
│   │   └── loki-config.yml               # Loki 3.x single-binary configuration
│   └── prometheus/
│       ├── Dockerfile                    # Prometheus v2.50.1 metrics image
│       ├── entrypoint.sh                 # Starts Prometheus scraping mcp-server:8001
│       └── prometheus.yml                # 10s scrape configuration for telecom-mcp-server
├── scripts/
│   └── reset_demo_data.py                # Restores PostgreSQL device states to seeded baseline
├── src/                                  # Application source code
│   ├── __init__.py
│   ├── core/
│   │   ├── audit.py                      # Async Loki audit log shipper
│   │   ├── config.py                     # Centralized Pydantic settings
│   │   └── security.py                   # Caller identity and header extraction
│   ├── crm/
│   │   ├── app.py                        # FastAPI Customer Care CRM Console (Port 8000)
│   │   ├── display_masking.py            # UI display masking helpers
│   │   ├── static/                       # Bundled Bootstrap 5 CSS/JS & Icons (offline-ready)
│   │   └── templates/                    # Jinja2 HTML templates (dashboard, customer_detail)
│   ├── db/
│   │   ├── database.py                   # Async SQLAlchemy engine & schema initialization
│   │   ├── demo_baselines.py             # Baseline device health & telemetry definitions
│   │   ├── models.py                     # ORM models (Customer, Account, Subscription, Device, etc.)
│   │   └── seed_data.py                  # Advisory-locked synthetic telecom dataset seeder
│   └── mcp/
│       ├── server.py                     # MCP JSON-RPC 2.0 & SSE server (Port 8001)
│       └── tools.py                      # 8 telecom customer-care MCP tools & Prometheus metrics
└── tests/
    └── test_telecom_mcp.py               # Automated unit and integration test suite
```

---

## Frameworks & Libraries

- **Application Runtime**: Python 3.11, FastAPI (`>=0.110.0`), Uvicorn (`>=0.28.0`), Pydantic Settings (`>=2.2.0`)
- **Database Layer**: PostgreSQL 16 (`postgres:16-alpine`), SQLAlchemy 2.0 Async (`>=2.0.28`), `asyncpg` (`>=0.29.0`)
- **CRM Frontend**: Jinja2 (`>=3.1.3`), Local Bootstrap 5.3 & Bootstrap Icons (zero external CDN dependencies)
- **Observability**: `prometheus-client` (`>=0.20.0`), `httpx` (`>=0.27.0`), Prometheus (`v2.50.1`), Grafana Loki (`3.0.0`), Grafana (`10.4.0`)
- **Container Operations**: Docker Engine, Docker Compose v2, Portainer CE (`portainer/portainer-ce:lts`)

---

## Environment Variables

All runtime settings are loaded from `.env` (created from `.env.example`). No secrets are hardcoded in the source code or container images.

| Variable | Type | Default / Example | Purpose |
| :--- | :--- | :--- | :--- |
| `BIND_ADDRESS` | `string` | `0.0.0.0` | Network interface bind address on the Ubuntu VM (`0.0.0.0` allows access from the customer network). |
| `POSTGRES_USER` | `string` | `telecom_user` | PostgreSQL database username. |
| `POSTGRES_PASSWORD` | `string` | `telecom_secure_pass` | **Required.** PostgreSQL password used by `postgres`, `mcp-server`, and `crm-app`. |
| `POSTGRES_DB` | `string` | `telecom_db` | PostgreSQL database name. |
| `POSTGRES_PORT` | `int` | `5432` | Host port exposed for the PostgreSQL database server. |
| `CRM_PORT` | `int` | `8000` | Host port exposed for the CRM Application web console. |
| `MCP_PORT` | `int` | `8001` | Host port exposed for the MCP Server. |
| `DEFAULT_END_USER_EMAIL` | `string` | `unknown-user@telecom.onprem` | Fallback caller identity recorded in Loki audit logs when no user header is supplied. |
| `PROMETHEUS_PORT` | `int` | `9090` | Host port exposed for Prometheus. |
| `LOKI_PORT` | `int` | `3100` | Host port exposed for Grafana Loki. |
| `LOKI_RETENTION_PERIOD` | `string` | `72h` | Audit log retention window in Loki (`72h` = 3 days) to bound disk consumption. |
| `GRAFANA_PORT` | `int` | `3000` | Host port exposed for the Grafana web UI. |
| `GRAFANA_ADMIN_USER` | `string` | `admin` | Grafana administrator username. |
| `GRAFANA_ADMIN_PASSWORD` | `string` | `telecom_admin` | **Required.** Grafana administrator password. |
| `PORTAINER_HTTPS_PORT` | `int` | `9443` | Host port exposed for the Portainer HTTPS UI. |
| `PORTAINER_HTTP_PORT` | `int` | `9000` | Host port exposed for the Portainer HTTP UI. |
| `PORTAINER_ADMIN_USER` | `string` | `admin` | Portainer administrator username (auto-initialized during `install.sh`). |
| `PORTAINER_ADMIN_PASSWORD` | `string` | `telecom_admin` | **Required (min 12 chars).** Portainer administrator password (auto-initialized during `install.sh` so Portainer never times out). |

---

## Ubuntu VM Installation Instructions

Run the following commands on an **Ubuntu 22.04 LTS or 24.04 LTS VM** in the customer environment.

### Step 1: Clone the Repository and (Optionally) Customize `.env`

If `.env` does not exist, `./install.sh` automatically creates it from `.env.example`. To clone the repository and optionally customize `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, or `PORTAINER_ADMIN_PASSWORD` prior to installation:

```bash
git clone https://github.com/pradeesi/TE.git
cd TE/On-prem
cp .env.example .env
nano .env
```

### Step 2: Run the One-Command Installer

```bash
chmod +x install.sh uninstall.sh
./install.sh
```

What `./install.sh` performs automatically:
1. **Checks & Installs Required Ubuntu OS Packages**: Verifies and installs any missing OS-level prerequisites (`ca-certificates`, `apt-transport-https`, `curl`, `wget`, `gnupg`, `lsb-release`, `jq`, `git`, `iproute2`, `netcat-openbsd`, `openssl`, `python3`, `python3-pip`, `python3-venv`), since minimal Ubuntu VM images may not have them pre-installed.
2. **Installs Host Python Dependencies (`.venv`)**: Creates a local Python virtual environment (`.venv`) and installs all packages from `requirements.txt` for host-level utility scripts and tests.
3. **Installs Docker Engine & Docker Compose v2**: Configures the official Ubuntu Docker APT repository and installs `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin`, and `docker-compose-plugin` if not already installed.
4. **Enables Boot Persistence (`VM Restart Events`)**: Enables `containerd.service` and `docker.service` via `systemd` so that all 7 containers (configured with `restart: always` in `docker-compose.yml`) automatically start whenever the Ubuntu VM restarts.
5. **Initializes `.env` & Configures Firewall (`ufw`)**: Creates `.env` from `.env.example` (if not already present), checks for host port conflicts, and opens TCP ports `8000`, `8001`, `3000`, `9090`, `3100`, `5432`, `9443`, and `9000` if `ufw` is active.
6. **Builds, Starts & Auto-Initializes All 7 Containers (Including Portainer Admin)**: Runs `docker compose up -d --build --remove-orphans`, verifies HTTP health checks across all services, and automatically initializes the Portainer CE administrator account via API (`PORTAINER_ADMIN_USER` / `PORTAINER_ADMIN_PASSWORD`) so Portainer **never** triggers the 5-minute uninitialized security timeout.

### Step 3: Verify Running Containers

```bash
docker compose ps
```

Expected output:

```text
NAME                 IMAGE                                   STATUS                   PORTS
telecom-crm-console  telecom-onprem/telecom-app:local        Up (healthy)             0.0.0.0:8000->8000/tcp
telecom-grafana      telecom-onprem/telecom-grafana:local    Up                       0.0.0.0:3000->3000/tcp
telecom-loki         telecom-onprem/telecom-loki:local       Up                       0.0.0.0:3100->3100/tcp
telecom-mcp-server   telecom-onprem/telecom-app:local        Up (healthy)             0.0.0.0:8001->8001/tcp
telecom-portainer    portainer/portainer-ce:lts              Up                       0.0.0.0:9000->9000/tcp, 0.0.0.0:9443->9443/tcp
telecom-postgres     postgres:16-alpine                      Up (healthy)             0.0.0.0:5432->5432/tcp
telecom-prometheus   telecom-onprem/telecom-prometheus:local Up                       0.0.0.0:9090->9090/tcp
```

---

## Accessing Components from the Customer Network

First, find the IP address of the Ubuntu VM on the customer network:

```bash
hostname -I | awk '{print $1}'
```

Replace `<UBUNTU_VM_IP>` below with the Ubuntu VM's IP address (for example, `10.20.30.40`).

### 1. Portainer CE (Container Status & Administration)
- **HTTPS URL**: `https://<UBUNTU_VM_IP>:9443`
- **HTTP URL**: `http://<UBUNTU_VM_IP>:9000`
- **Credentials**: Username from `PORTAINER_ADMIN_USER` (default `admin`) and password from `PORTAINER_ADMIN_PASSWORD` (default `telecom_admin`) in `.env`.
- **Usage**:
  1. Open `https://<UBUNTU_VM_IP>:9443` (or `http://<UBUNTU_VM_IP>:9000`) in your browser and sign in with `admin` / `telecom_admin` (pre-initialized automatically by `./install.sh` so the 5-minute setup timeout never occurs).
  2. Click **Get Started** -> select the **local** Docker environment -> click **Containers** to inspect live health status (`healthy` / `running`), container logs, CPU/RAM metrics, and restart controls for all 7 containers.

### 2. CRM Application (Customer Care Agent Console)
- **Web URL**: `http://<UBUNTU_VM_IP>:8000`
- **Health Check**: `http://<UBUNTU_VM_IP>:8000/health`
- **Usage**: Browse the subscriber queue, view Customer 360 profiles, inspect live ONT/5G device telemetry, review 6-month itemized billing records, and execute remote device actions.

### 3. MCP Server (Model Context Protocol Server)
- **MCP JSON-RPC Endpoint**: `http://<UBUNTU_VM_IP>:8001/mcp`
- **MCP SSE Stream Endpoint**: `http://<UBUNTU_VM_IP>:8001/sse`
- **REST Tool Catalog**: `http://<UBUNTU_VM_IP>:8001/tools`
- **REST Direct Execution**: `http://<UBUNTU_VM_IP>:8001/execute`
- **Prometheus Metrics**: `http://<UBUNTU_VM_IP>:8001/metrics`
- **Health Check**: `http://<UBUNTU_VM_IP>:8001/health`
- **Apigee Hybrid Target Configuration**: Configure your customer-managed Apigee Hybrid proxy TargetEndpoint `<URL>` to `http://<UBUNTU_VM_IP>:8001` (Apigee automatically appends `/mcp` from `proxy.pathsuffix`). See **[`APIGEE_CONFIGURATION_GUIDE.md`](APIGEE_CONFIGURATION_GUIDE.md)** for the complete, copy-pasteable Apigee proxy XML/JS bundle, Model Armor setup, and verification commands.

### 4. Grafana (Observability & Audit Dashboards)
- **Web URL**: `http://<UBUNTU_VM_IP>:3000`
- **Credentials**: Username from `GRAFANA_ADMIN_USER` (default `admin`) and password from `GRAFANA_ADMIN_PASSWORD` in `.env`.
- **Preloaded Dashboards**: Navigate to **Dashboards -> HybridMCP-Apigee** to open:
  - **Telecom MCP Security & Audit Trail**
  - **AI Agent Observability**

### 5. Prometheus (Metrics Explorer)
- **Web URL**: `http://<UBUNTU_VM_IP>:9090`
- **Target Status**: `http://<UBUNTU_VM_IP>:9090/targets` (verifies `telecom-mcp-server` scrape health).

### 6. Grafana Loki (Audit Log Ingestion API)
- **Readiness Endpoint**: `http://<UBUNTU_VM_IP>:3100/ready`
- **Push API (for Apigee Hybrid Masked Logs)**: `http://<UBUNTU_VM_IP>:3100/loki/api/v1/push`

### 7. PostgreSQL Database Server
- **Host / Port**: `<UBUNTU_VM_IP>:5432`
- **Database / User**: `telecom_db` / `telecom_user` (or values configured in `.env`)
- **Connection URI**: `postgresql://telecom_user:<POSTGRES_PASSWORD>@<UBUNTU_VM_IP>:5432/telecom_db`

---

## Connecting to the MCP Server from Postman

You can connect to and test the MCP Server from **Postman** using either **Postman's Native MCP Client** or **Standard HTTP Requests (JSON-RPC 2.0 / REST)**.

### Pre-Seeded Test Subscribers

Use any of the following pre-seeded subscriber phone numbers when testing tools in Postman:

| Subscriber Name | Phone Number | Account ID | Scenario / Persona |
| :--- | :--- | :--- | :--- |
| **Elena Rostova** | `+15552345678` | `TEL-ACC-88129` | Degraded Fiber 500 ONT router (`-28.5 dBm` optical Rx power, `14.2%` packet loss). |
| **Marcus Vance** | `+15559871234` | `TEL-ACC-99214` | Mobile 5G line throttled to `128 kbps` after exceeding the `50 GB` data cap (`54.8 GB` used). |
| **Amina Al-Mansoor** | `+15556784321` | `TEL-ACC-44910` | Platinum subscriber disputing an `$85.00` international roaming fee on her August 2026 bill. |
| **David Chen** | `+15553127890` | `TEL-ACC-11029` | Fiber 100 subscriber at `94.6%` bandwidth utilization (prime Gigabit Fiber upsell candidate). |
| **Mallory Tester** | `+15550001337` | `TEL-ACC-01337` | Security test persona containing an embedded prompt-injection and Safe Browsing test URL payload. |

---

### Method 1: Using Postman's Built-In MCP Client

1. Open **Postman** and click **New** -> **MCP Request**.
2. Select the transport type:
   - **HTTP (Streamable HTTP / JSON-RPC)**: Enter `http://<UBUNTU_VM_IP>:8001/mcp`
   - **SSE (Server-Sent Events)**: Enter `http://<UBUNTU_VM_IP>:8001/sse`
3. *(Optional)* Open the **Headers** tab and add caller attribution headers for the Loki audit trail:
   - `x-end-user-email`: `operator@customer.com`
   - `x-agent-id`: `postman-mcp-client`
4. Click **Connect** (or **Load Tools**). Postman will perform the MCP `initialize` and `tools/list` handshake and display all 8 telecom tools in the sidebar.
5. Select a tool (for example, `get_customer_360`), enter the JSON arguments below, and click **Run**:
   ```json
   {
     "phone_number": "+15552345678"
   }
   ```

---

### Method 2: Using Standard Postman HTTP Requests (JSON-RPC 2.0)

In Postman, create a standard **HTTP Request** with:
- **Method**: `POST`
- **URL**: `http://<UBUNTU_VM_IP>:8001/mcp`
- **Headers**:
  - `Content-Type`: `application/json`
  - `x-end-user-email`: `operator@customer.com` *(optional, recorded in Grafana Loki audit trail)*
- **Body**: Select **raw** -> **JSON** and paste any of the payloads below.

#### 1. Initialize MCP Session (`initialize`)
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {
      "name": "postman-client",
      "version": "1.0.0"
    }
  }
}
```

#### 2. List Available MCP Tools (`tools/list`)
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}
```

#### 3. Call `get_customer_360` (`tools/call`)
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "get_customer_360",
    "arguments": {
      "phone_number": "+15552345678"
    }
  }
}
```

#### 4. Call `get_service_diagnostics` (`tools/call`)
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "get_service_diagnostics",
    "arguments": {
      "phone_number": "+15552345678"
    }
  }
}
```

#### 5. Call `run_remote_device_action` (`tools/call`)
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "run_remote_device_action",
    "arguments": {
      "phone_number": "+15552345678",
      "action": "reboot"
    }
  }
}
```

#### 6. Call `get_billing_breakdown` (`tools/call`)
```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "get_billing_breakdown",
    "arguments": {
      "phone_number": "+15556784321"
    }
  }
}
```

#### 7. Call `check_network_outages` (`tools/call`)
```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "check_network_outages",
    "arguments": {
      "postal_code": "98101"
    }
  }
}
```

#### 8. Call `get_upsell_recommendations` (`tools/call`)
```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "get_upsell_recommendations",
    "arguments": {
      "phone_number": "+15553127890"
    }
  }
}
```

#### 9. Call `log_agent_interaction` (`tools/call`)
```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "log_agent_interaction",
    "arguments": {
      "phone_number": "+15552345678",
      "agent_name": "Postman Operator",
      "issue_summary": "Customer reported evening Wi-Fi buffering.",
      "resolution_summary": "Ran diagnostics and executed remote ONT reboot.",
      "call_duration_sec": 180,
      "upsell_offered": true,
      "upsell_accepted": false
    }
  }
}
```

---

### Method 3: Using the Direct REST Bridge Endpoints in Postman

For quick REST testing without a JSON-RPC envelope:

- **List Tools (`GET`)**:
  - **URL**: `http://<UBUNTU_VM_IP>:8001/tools`
- **Execute Tool (`POST`)**:
  - **URL**: `http://<UBUNTU_VM_IP>:8001/execute`
  - **Headers**: `Content-Type: application/json`
  - **Body**:
    ```json
    {
      "name": "search_customer",
      "arguments": {
        "phone_number": "+15552345678"
      }
    }
    ```

---

## Resetting Demo Data on the Ubuntu VM

After running mutating tools (such as rebooting Elena Rostova's router via `run_remote_device_action` or logging new call interactions), you can restore all devices and call logs to their initial seeded state at any time:

```bash
docker exec telecom-mcp-server python3 -m scripts.reset_demo_data --purge-rehearsal-interactions
```

---

## Uninstalling the Stack from the Ubuntu VM

Use `./uninstall.sh` to remove only the application components (the 7 containers, `telecom-net` network, locally built `telecom-onprem/*` images, `.venv`, UFW port rules, and optionally the named data volumes). **All OS-level prerequisites (Docker Engine, Docker Compose, Python 3, and Ubuntu system packages) are left intact** so you can re-install at any time:

```bash
# Full application teardown (removes containers, images, .venv, and named data volumes; keeps OS packages)
./uninstall.sh --yes

# Stop and remove containers while preserving PostgreSQL, Loki, Prometheus, Grafana, and Portainer volumes
./uninstall.sh --keep-data --yes

# Full application teardown and also delete the local .env file
./uninstall.sh --remove-env --yes
```

