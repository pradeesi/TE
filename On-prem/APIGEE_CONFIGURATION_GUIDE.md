# Apigee Southbound Configuration Guide (Apigee -> On-Premises MCP Server)

<!--
Purpose: Southbound configuration specification for Apigee Engineers connecting an existing Apigee Hybrid or Apigee X proxy to the On-Premises Ubuntu VM MCP Server.
Architecture/Context: Covers strictly the Apigee-to-MCP (southbound) connection, required HTTP target properties, expected MCP routes/protocols, and optional observability header/log forwarding. Northbound Apigee configuration is managed independently by the Apigee team.
Dependencies/Side Effects: None. Purely declarative southbound reference and copy-pasteable Apigee TargetEndpoint XML artifacts.
-->

This document is intended for the **Apigee Engineering Team**. It provides the exact **southbound (`Apigee -> MCP Server`)** configuration details required to connect an Apigee API Proxy to the **On-Premises Telecom MCP Server** running on the Ubuntu VM.

---

## 1. Southbound Architecture Overview

```
+-------------------------------------+                  +--------------------------------------------------+
|   Apigee Runtime (Customer VMs)     |                  |   On-Premises Ubuntu VM (Docker Compose Stack)   |
|                                     |                  |                                                  |
|  +-------------------------------+  |   Primary (TCP)  |  +--------------------------------------------+  |
|  |     TargetEndpoint            |==+==================+=>|  MCP Server (`telecom-mcp-server`)         |  |
|  |     (Southbound HTTP)         |  |   Port: 8001     |  |  - POST /mcp     (JSON-RPC 2.0 Execution)  |  |
|  +---------------+---------------+  |                  |  |  - GET  /sse     (SSE Stream / Discovery)  |  |
|                  |                  |                  |  |  - GET  /health  (Liveness Probe)          |  |
|                  | (Optional)       |                  |  +--------------------------------------------+  |
|                  | Audit Push       |                  |                                                  |
|                  v                  |   Optional (TCP) |  +--------------------------------------------+  |
|  +-------------------------------+  |   Port: 3100     |  |  Grafana Loki (`telecom-loki`)             |  |
|  |  ServiceCallout (SC-LokiAudit)|==+==================+=>|  - POST /loki/api/v1/push                  |  |
|  +-------------------------------+  |                  |  +--------------------------------------------+  |
+-------------------------------------+                  +--------------------------------------------------+
```

---

## 2. Southbound MCP Server Connection Details (Primary)

### 2.1 Network & Endpoint Summary

| Setting | Value | Notes |
| :--- | :--- | :--- |
| **Target Host / IP** | `<UBUNTU_VM_IP>` | Replace with the Ubuntu VM's IP address on the customer network (e.g., `10.128.0.2` or `136.115.24.106`). |
| **Target Port** | `8001` (TCP) | Exposed by the `telecom-mcp-server` container (`0.0.0.0:8001`). |
| **Target Protocol** | `HTTP/1.1` | Plain HTTP over the internal data center network. |
| **Target Base URL** | `http://<UBUNTU_VM_IP>:8001` | **Important**: Set `<URL>` to `http://<UBUNTU_VM_IP>:8001` (without `/mcp`) when Apigee's default `copy.pathsuffix=true` is enabled, or set `<URL>` to `http://<UBUNTU_VM_IP>:8001/mcp` if `target.copy.pathsuffix=false`. |
| **Southbound Authentication** | **None Required** | Do **not** configure `<GoogleIDToken>` or `<GoogleAccessToken>` on the MCP TargetEndpoint. The on-premises MCP Server does not require a Cloud Run IAM token. Strip any client credentials (`x-apikey`, client `Authorization`) before forwarding to the target. |

---

### 2.2 Supported MCP Server Routes & Methods

The MCP Server on port `8001` exposes the following endpoints that Apigee can route to:

| Route Path | HTTP Method | Content-Type | Purpose |
| :--- | :--- | :--- | :--- |
| **`/mcp`** *(Primary)* | `POST` | `application/json` | **MCP JSON-RPC 2.0 Endpoint** (MCP Protocol `2024-11-05`). Accepts single or batch JSON-RPC 2.0 requests (`initialize`, `notifications/initialized`, `ping`, `tools/list`, and `tools/call`) and returns the JSON-RPC 2.0 response synchronously in the HTTP body. |
| **`/health`** | `GET` | `application/json` | **Health / Liveness Probe**. Returns HTTP `200 OK` with `{"status":"healthy","service":"telecom-mcp-server","version":"2.0.0"}`. Can be used by Apigee `HealthMonitor` / TargetServer health checks. |
| **`/sse`** *(Optional)* | `GET` | `text/event-stream` | **MCP Server-Sent Events (SSE) Discovery Endpoint**. When called with `Accept: text/event-stream`, opens a streaming connection that emits `event: endpoint` (`{"type":"endpoint","endpoint":"/mcp"}`) and 15-second `: ping` heartbeats. *(Not needed if your MCP client uses standard `POST /mcp` Streamable HTTP).* |
| **`/tools`** *(Optional)* | `GET` | `application/json` | Convenience REST endpoint that returns the JSON schema catalog of all 8 MCP tools. |
| **`/execute`** *(Optional)* | `POST` | `application/json` | Convenience REST bridge (`{"name":"<tool_name>","arguments":{...}}`) for non-JSON-RPC HTTP callers. |

---

### 2.3 Copy-Pasteable Apigee `TargetEndpoint` Configuration

Use either **Option A** (Direct URL in `TargetEndpoint`) or **Option B** (Environment-scoped Apigee `TargetServer` with Health Check).

#### Option A: Direct `TargetEndpoint` XML (`apiproxy/targets/mcp-onprem.xml`)

This is the exact southbound `TargetEndpoint` configuration to connect Apigee to the Ubuntu VM MCP Server:

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<TargetEndpoint name="mcp-onprem">
  <Description>Southbound connection to On-Premises Ubuntu VM FastMCP Server (Port 8001).</Description>
  <PreFlow name="PreFlow">
    <Request/>
    <Response/>
  </PreFlow>
  <Flows/>
  <PostFlow name="PostFlow">
    <Request/>
    <Response/>
  </PostFlow>
  <HTTPTargetConnection>
    <Properties>
      <!-- Keep 30s read timeout and 5s connect timeout for database/tool queries -->
      <Property name="io.timeout.millis">30000</Property>
      <Property name="connect.timeout.millis">5000</Property>
      <!-- Optional: Include 4xx/5xx in success.codes if you attach response sanitization policies in Target Response Flow -->
      <Property name="success.codes">1xx,2xx,3xx,4xx,5xx</Property>
    </Properties>
    <!-- Replace <UBUNTU_VM_IP> with the Ubuntu VM IP (e.g., http://10.128.0.2:8001 or http://136.115.24.106:8001).
         NOTE: By default Apigee copies proxy.pathsuffix (/mcp or /health) to the target URL.
         Therefore, do NOT append /mcp here unless you explicitly set target.copy.pathsuffix=false. -->
    <URL>http://<UBUNTU_VM_IP>:8001</URL>
  </HTTPTargetConnection>
</TargetEndpoint>
```

> **Path Suffix Handling Note for the Apigee Engineer**:
> - **Case 1 (Default Apigee Behavior — `target.copy.pathsuffix = true`)**: If the client calls Apigee at `POST https://<APIGEE_HOST>/<BASEPATH>/mcp`, Apigee sets `proxy.pathsuffix = /mcp` and automatically appends `/mcp` to `<URL>http://<UBUNTU_VM_IP>:8001</URL>`, resulting in `POST http://<UBUNTU_VM_IP>:8001/mcp`.
> - **Case 2 (Fixed Target Path — `target.copy.pathsuffix = false`)**: If your proxy maps the root basepath directly to the MCP endpoint without a `/mcp` suffix on the northbound URL, disable `target.copy.pathsuffix` in a Target Request PreFlow `AssignMessage` policy and set `<URL>http://<UBUNTU_VM_IP>:8001/mcp</URL>`:
>   ```xml
>   <AssignMessage name="AM-DisablePathSuffix">
>     <AssignVariable>
>       <Name>target.copy.pathsuffix</Name>
>       <Value>false</Value>
>     </AssignVariable>
>   </AssignMessage>
>   ```

---

#### Option B: Using an Apigee `TargetServer` (With Built-in `/health` Monitoring)

If your Apigee team prefers environment-scoped `TargetServers` instead of hardcoding the IP in the proxy bundle:

1. **Create the `TargetServer` in the Apigee Environment (`Admin -> Environments -> Target Servers`)**:
   ```json
   {
     "name": "telecom-mcp-onprem",
     "host": "<UBUNTU_VM_IP>",
     "port": 8001,
     "isEnabled": true,
     "protocol": "HTTP"
   }
   ```
2. **Reference `telecom-mcp-onprem` in `TargetEndpoint` with `HealthMonitor`**:
   ```xml
   <HTTPTargetConnection>
     <LoadBalancer>
       <Server name="telecom-mcp-onprem"/>
     </LoadBalancer>
     <Path>/mcp</Path>
     <Properties>
       <Property name="io.timeout.millis">30000</Property>
       <Property name="connect.timeout.millis">5000</Property>
     </Properties>
     <HealthMonitor>
       <IsEnabled>true</IsEnabled>
       <IntervalInSec>10</IntervalInSec>
       <HTTPMonitor>
         <Request>
           <ConnectTimeoutInSec>5</ConnectTimeoutInSec>
           <SocketReadTimeoutInSec>5</SocketReadTimeoutInSec>
           <Port>8001</Port>
           <Verb>GET</Verb>
           <Path>/health</Path>
         </Request>
         <SuccessResponse>
           <ResponseCode>200</ResponseCode>
         </SuccessResponse>
       </HTTPMonitor>
     </HealthMonitor>
   </HTTPTargetConnection>
   ```

---

### 2.4 Southbound Request & Response Payload Contract (`POST /mcp`)

#### Expected Southbound Request Sent from Apigee to `http://<UBUNTU_VM_IP>:8001/mcp`
- **Method**: `POST`
- **Headers**:
  - `Content-Type: application/json`
- **Body (Standard MCP JSON-RPC 2.0)**:
  ```json
  {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "get_customer_360",
      "arguments": {
        "phone_number": "+1 (555) 234-5678"
      }
    }
  }
  ```

#### Southbound Response Returned by the MCP Server to Apigee
- **Status**: `200 OK` (or `204 No Content` for JSON-RPC notifications that have no `id`)
- **Headers**: `Content-Type: application/json`
- **Body**:
  ```json
  {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
      "content": [
        {
          "type": "text",
          "text": "{\"status\": \"success\", \"customer_360\": { ... }}"
        }
      ]
    }
  }
  ```
*(Note: The MCP Server returns **raw, unmasked subscriber data** inside `result.content[0].text`. If your Apigee proxy enforces **Google Cloud Model Armor / SDP** on the Target Response Flow, inspect and replace `result.content[*].text` before returning the response to the caller).*

---

### 2.5 Quick Connectivity Verification from the Apigee Runtime VMs

Run these two `curl` commands from your Apigee Hybrid runtime VM (or cluster node) to verify network reachability to the Ubuntu VM before deploying the proxy:

```bash
export UBUNTU_VM_IP="<UBUNTU_VM_IP>"

# 1. Verify TCP 8001 Health Check
curl -i "http://${UBUNTU_VM_IP}:8001/health"

# 2. Verify MCP JSON-RPC Tool Call
curl -s -X POST "http://${UBUNTU_VM_IP}:8001/mcp" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "search_customer",
      "arguments": {
        "phone_number": "+1 (555) 234-5678"
      }
    }
  }' | jq .
```

---

## 3. Optional: Observability & Audit Integration

The MCP Server on the Ubuntu VM **automatically** records all incoming MCP requests and tool executions into its local Grafana Loki instance (`{job="telecom-mcp-audit"}`) and exposes Prometheus metrics (`:8001/metrics`). **No extra observability configuration is required on Apigee for the stack and Grafana dashboards to work.**

However, if the Apigee team wants to enrich the Grafana dashboards with Apigee caller metadata or Model Armor security verdicts, two **completely optional** integrations are supported below.

---

### 3.1 Optional A: Forwarding Apigee Identity & Trace Headers to the MCP Server

When the MCP Server receives a request on `http://<UBUNTU_VM_IP>:8001/mcp`, it automatically checks for the following optional HTTP headers and includes them in its Loki audit logs and Grafana dashboards (defaulting to `"N/A"` or `"telecom-adk-agent"` if omitted):

| Optional Southbound Header | Description | Example Value in Apigee |
| :--- | :--- | :--- |
| `X-Apigee-Developer-App` | Name of the Apigee Developer App that called the proxy | `{verifyapikey.VA-VerifyApiKey.app.name}` or `{developer.app.name}` |
| `X-Apigee-Client-Id` | Consumer Key / Client ID of the Apigee Developer App | `{verifyapikey.VA-VerifyApiKey.client_id}` or `{client_id}` |
| `X-Apigee-Proxy` | Name of the Apigee API Proxy | `{apiproxy.name}` |
| `X-Apigee-Message-Id` | Apigee unique request message ID | `{messageid}` |
| `X-End-User-Email` | Email of the human operator / frontline agent (if applicable) | `sarah.jenkins@telecom.example` |
| `X-Agent-Identity` | Logical name of the calling AI agent | `telecom-adk-agent` |
| `X-Cloud-Trace-Context` or `X-Trace-Id` | Correlation / distributed trace ID | `{messageid}` |

**Optional `AssignMessage` Policy (`AM-TargetIdentityHeaders.xml` in Target Request PreFlow)**:
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<AssignMessage continueOnError="true" enabled="true" name="AM-TargetIdentityHeaders">
  <DisplayName>AM-TargetIdentityHeaders</DisplayName>
  <Set>
    <Headers>
      <Header name="X-Apigee-Developer-App">{verifyapikey.VA-VerifyApiKey.app.name}</Header>
      <Header name="X-Apigee-Client-Id">{verifyapikey.VA-VerifyApiKey.client_id}</Header>
      <Header name="X-Apigee-Proxy">{apiproxy.name}</Header>
      <Header name="X-Apigee-Message-Id">{messageid}</Header>
    </Headers>
  </Set>
  <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>
  <AssignTo createNew="false" transport="http" type="request"/>
</AssignMessage>
```

---

### 3.2 Optional B: Pushing Apigee Model Armor Verdicts to Grafana Loki (Port `3100`)

The Grafana dashboards on the Ubuntu VM (`http://<UBUNTU_VM_IP>:3000`) include a section titled **"Apigee Model Armor Security"** that queries `{app="apigee-mcp-proxy"}` in Loki.

- **If Apigee does NOT push logs to Loki**: Those panels cleanly display `0` / `No data` without throwing any errors in Grafana or Apigee.
- **If you want to populate those panels**: Add a best-effort (`continueOnError="true"`) `ServiceCallout` in your Apigee Proxy `PostFlow` that `POST`s to `http://<UBUNTU_VM_IP>:3100/loki/api/v1/push` (no authentication header required):

#### 1. Southbound Loki Push Endpoint
- **URL**: `http://<UBUNTU_VM_IP>:3100/loki/api/v1/push`
- **Method**: `POST`
- **Headers**: `Content-Type: application/json`
- **Authentication**: None required (`LOKI_AUTH_MODE=none` on the Ubuntu VM).

#### 2. Optional `ServiceCallout` Policy (`SC-LokiAudit.xml`)
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ServiceCallout continueOnError="true" enabled="true" name="SC-LokiAudit">
  <DisplayName>SC-LokiAudit</DisplayName>
  <Request clearPayload="true" variable="lokiRequest">
    <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>
    <Set>
      <Headers>
        <Header name="Content-Type">application/json</Header>
      </Headers>
      <Payload contentType="application/json">{hmcp.loki_payload}</Payload>
      <Verb>POST</Verb>
    </Set>
  </Request>
  <Response>lokiResponse</Response>
  <HTTPTargetConnection>
    <Properties>
      <Property name="io.timeout.millis">2000</Property>
      <Property name="connect.timeout.millis">1000</Property>
    </Properties>
    <!-- Replace <UBUNTU_VM_IP> with the Ubuntu VM IP -->
    <URL>http://<UBUNTU_VM_IP>:3100/loki/api/v1/push</URL>
  </HTTPTargetConnection>
</ServiceCallout>
```

#### 3. Expected Loki JSON Payload Format (`{hmcp.loki_payload}`)
```json
{
  "streams": [
    {
      "stream": {
        "app": "apigee-mcp-proxy",
        "apigee_env": "prod",
        "action": "MASKED"
      },
      "values": [
        [
          "<NANOSECOND_UNIX_EPOCH_STRING>",
          "{\"event_type\":\"APIGEE_MODEL_ARMOR\",\"action\":\"MASKED\",\"tool_name\":\"get_customer_360\",\"jsonrpc_method\":\"tools/call\",\"jsonrpc_id\":\"1\",\"developer_app\":\"telecom-adk-agent-app\",\"client_id\":\"...\",\"end_user_email\":\"sarah.jenkins@telecom.example\",\"trace_id\":\"...\",\"upstream_status\":\"200\",\"pi_jailbreak_match\":false,\"malicious_uri_match\":false,\"sdp_match\":true,\"ma_invocation\":\"SUCCESS\",\"ma_error\":\"\",\"latency_ms\":145,\"masked_response\":\"...\"}"
        ]
      ]
    }
  ]
}
```
*(Valid `action` label values recognized by the Grafana dashboard: `PASSED`, `MASKED`, `BLOCKED`, `MA_ERROR`).*
