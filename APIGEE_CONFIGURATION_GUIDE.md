# Apigee Configuration Guide (`telecom-mcp` Proxy -> On-Premises Ubuntu VM)

<!--
Purpose: Step-by-step, foolproof reference guide for configuring Apigee Hybrid or Apigee X to connect to the On-Premises Ubuntu VM MCP Server and (optionally) Grafana Loki.
Architecture/Context: Bridges the customer-managed Apigee runtime with the Docker Compose stack deployed from this repository (MCP Server on port 8001, Grafana Loki on port 3100).
Dependencies/Side Effects: None. Purely declarative documentation and copy-pasteable Apigee XML/JS/CLI artifacts.
-->

This guide provides **complete, copy-pasteable configuration files and step-by-step instructions** to configure **Apigee Hybrid** (or **Apigee X**) to govern, authenticate, sanitize (via **Model Armor + Cloud Sensitive Data Protection**), and route AI Agent requests to the **MCP Server** running on your Ubuntu VM.

---

## 1. End-to-End Request & Response Flow

```
+----------------------------------------------------------------------------------------------------------+
| 1. AI Agent / Agent Gateway                                                                              |
|    POST https://<APIGEE_HOST>/telecom-mcp/mcp                                                            |
|    Headers:                                                                                              |
|      x-apikey: <APIGEE_CONSUMER_KEY>                                                                     |
|      x-end-user-email: sarah.jenkins@telecom.example                                                     |
|      Content-Type: application/json                                                                      |
+----------------------------------------------------+-----------------------------------------------------+
                                                     |
                                                     v
+----------------------------------------------------------------------------------------------------------+
| 2. Apigee Hybrid / Apigee X (`telecom-mcp` API Proxy)                                                    |
|                                                                                                          |
|  [Northbound Request PreFlow]                                                                            |
|    a. SA-SpikeArrest             -> Rate-limits traffic (e.g., 20ps) to protect the Ubuntu VM.           |
|    b. VA-VerifyApiKey            -> Validates `x-apikey` against Apigee API Product & Developer App.     |
|    c. AM-StripClientCredentials  -> Strips `x-apikey` and client `Authorization` headers.                |
|    d. JS-CaptureRequest          -> Extracts JSON-RPC method, id, tool_name, and end_user_email.         |
|                                                                                                          |
|  [Southbound Target Request PreFlow]                                                                     |
|    e. AM-TargetIdentityHeaders   -> Injects verified headers (`X-Apigee-Developer-App`,                  |
|                                     `X-Apigee-Client-Id`, `X-Apigee-Proxy`, `X-Apigee-Message-Id`).      |
+----------------------------------------------------+-----------------------------------------------------+
                                                     |
                                                     |  POST http://<UBUNTU_VM_IP>:8001/mcp
                                                     v
+----------------------------------------------------------------------------------------------------------+
| 3. On-Premises Ubuntu VM: MCP Server (`telecom-mcp-server` on Port 8001)                                 |
|    - Queries PostgreSQL 16 (`telecom-postgres:5432`) and returns RAW JSON-RPC tool output.               |
|    - Automatically streams the full raw request/response audit log + Apigee headers to local Loki        |
|      (`http://loki:3100/loki/api/v1/push` under `{job="telecom-mcp-audit"}`).                            |
+----------------------------------------------------+-----------------------------------------------------+
                                                     |
                                                     |  RAW JSON-RPC Response
                                                     v
+----------------------------------------------------------------------------------------------------------+
| 4. Apigee Hybrid / Apigee X (`telecom-mcp` Target Response Flow & PostFlow)                              |
|                                                                                                          |
|  [Southbound Target Response PreFlow]                                                                    |
|    f. JS-PrepareModelArmor       -> Extracts text segments from the MCP JSON-RPC response.               |
|    g. SC-ModelArmor              -> Calls Google Cloud Model Armor (`sanitizeModelResponse`) to detect   |
|                                     Prompt Injection, Malicious URIs, and mask 7 PII infoTypes via SDP.  |
|    h. JS-ApplyModelArmor         -> Replaces raw PII with masked tokens (`[PERSON_NAME]`, `[EMAIL]`,     |
|                                     etc.), or blocks prompt-injection responses with JSON-RPC `-32001`   |
|                                     (and fails closed with `-32002` if Model Armor is unreachable).      |
|                                                                                                          |
|  [Proxy Response PostFlow - OPTIONAL]                                                                    |
|    i. JS-BuildLokiPayload        -> Formats the masked Model Armor verdict for Grafana.                  |
|    j. SC-LokiAudit (Optional)    -> Best-effort (`continueOnError="true"`) push to                       |
|                                     `http://<UBUNTU_VM_IP>:3100/loki/api/v1/push` (`{app="apigee-mcp-proxy"}`).
+----------------------------------------------------------------------------------------------------------+
```

---

## 2. Key Configuration Parameters Summary

Before creating the proxy, note the exact values from your Ubuntu VM deployment:

| Parameter | Placeholder in XML | Value to Use | Description |
| :--- | :--- | :--- | :--- |
| **Ubuntu VM IP** | `<UBUNTU_VM_IP>` | e.g., `136.115.24.106` (External) or `10.128.0.2` (Internal VPC) | IP address of the Ubuntu VM running the Docker Compose stack. |
| **MCP Target URL** | `${MCP_SERVICE_URL}` | `http://<UBUNTU_VM_IP>:8001` | **Important**: Do **not** add `/mcp` at the end of `<URL>` in `TargetEndpoint` because Apigee automatically appends `proxy.pathsuffix` (`/mcp` or `/health`). |
| **Loki Push URL (Optional)** | `${LOKI_URL}` | `http://<UBUNTU_VM_IP>:3100` | Used by `SC-LokiAudit.xml` to push Model Armor verdicts (`MASKED` / `BLOCKED`) to Grafana. Completely optional. |
| **Proxy Base Path** | `${APIGEE_PROXY_BASEPATH}` | `/telecom-mcp` | Base path exposed by Apigee (`https://<APIGEE_HOST>/telecom-mcp/mcp`). |
| **Spike Arrest Rate** | `${APIGEE_SPIKE_ARREST_RATE}` | `20ps` | Maximum request rate allowed through the proxy (20 requests per second). |
| **Apigee Environment** | `${APIGEE_ENV}` | e.g., `eval` or `prod` | Name of your Apigee deployment environment. |
| **Model Armor URL** | `${MODEL_ARMOR_SANITIZE_URL}` | `https://modelarmor.<LOCATION>.rep.googleapis.com/v1/projects/<PROJECT_ID>/locations/<LOCATION>/templates/<TEMPLATE_ID>:sanitizeModelResponse` | Regional Model Armor endpoint (if using Model Armor response sanitization). |

---

## 3. Choosing Your Apigee Configuration Mode

You can deploy the Apigee proxy in **two modes** depending on your environment:

- **Mode A — Standard Gateway Mode (Pass-Through + API Key Auth + Rate Limiting + Audit Attribution)**
  - Ideal if you want to immediately connect Apigee to the Ubuntu VM MCP Server **without** setting up Google Cloud Model Armor templates first.
  - Enforces `SA-SpikeArrest`, `VA-VerifyApiKey`, `AM-StripClientCredentials`, and `AM-TargetIdentityHeaders`, and routes `POST /mcp` and `GET /health` to `http://<UBUNTU_VM_IP>:8001`.
- **Mode B — Full Model Armor + SDP Governance Mode (Recommended for Production / Security Demo)**
  - Includes everything in Mode A **plus** inline response sanitization via Google Cloud Model Armor (`JS-PrepareModelArmor`, `SC-ModelArmor`, `JS-ApplyModelArmor`) and optional Loki security audit shipping (`JS-BuildLokiPayload`, `SC-LokiAudit`).

Both modes are documented in full below.

---

## 4. Complete Apigee Proxy Bundle Structure (`apiproxy/`)

Whether you import the proxy via the Apigee UI (**Develop -> API Proxies -> + Create -> Upload proxy bundle**) or via the Apigee REST API, the zip bundle has this structure:

```text
apiproxy/
├── telecom-mcp.xml
├── proxies/
│   └── default.xml
├── targets/
│   └── mcp-onprem.xml
├── policies/
│   ├── AM-FaultJson.xml
│   ├── AM-StripClientCredentials.xml
│   ├── AM-TargetIdentityHeaders.xml
│   ├── JS-ApplyModelArmor.xml        # (Mode B only)
│   ├── JS-BuildLokiPayload.xml       # (Optional - Mode B)
│   ├── JS-CaptureRequest.xml
│   ├── JS-PrepareModelArmor.xml      # (Mode B only)
│   ├── RF-NotFound.xml
│   ├── SA-SpikeArrest.xml
│   ├── SC-LokiAudit.xml              # (Optional - Mode B)
│   ├── SC-ModelArmor.xml             # (Mode B only)
│   └── VA-VerifyApiKey.xml
└── resources/
    └── jsc/
        ├── apply-model-armor.js      # (Mode B only)
        ├── build-loki-payload.js     # (Optional - Mode B)
        ├── capture-request.js
        └── prepare-model-armor.js    # (Mode B only)
```

---

## 5. Copy-Pasteable XML & JavaScript Files

Replace `136.115.24.106` in the snippets below with your Ubuntu VM's IP address (`<UBUNTU_VM_IP>`).

### 5.1 Proxy Descriptor: `apiproxy/telecom-mcp.xml`

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<APIProxy revision="1" name="telecom-mcp">
  <DisplayName>Telecom MCP (On-Premises Ubuntu VM)</DisplayName>
  <Description>Governed MCP JSON-RPC proxy connecting Apigee to the on-premises Ubuntu VM MCP Server.</Description>
  <Basepaths>/telecom-mcp</Basepaths>
  <Policies>
    <Policy>SA-SpikeArrest</Policy>
    <Policy>VA-VerifyApiKey</Policy>
    <Policy>AM-StripClientCredentials</Policy>
    <Policy>JS-CaptureRequest</Policy>
    <Policy>AM-TargetIdentityHeaders</Policy>
    <Policy>JS-PrepareModelArmor</Policy>
    <Policy>SC-ModelArmor</Policy>
    <Policy>JS-ApplyModelArmor</Policy>
    <Policy>JS-BuildLokiPayload</Policy>
    <Policy>SC-LokiAudit</Policy>
    <Policy>RF-NotFound</Policy>
    <Policy>AM-FaultJson</Policy>
  </Policies>
  <ProxyEndpoints>
    <ProxyEndpoint>default</ProxyEndpoint>
  </ProxyEndpoints>
  <Resources>
    <Resource>jsc://capture-request.js</Resource>
    <Resource>jsc://prepare-model-armor.js</Resource>
    <Resource>jsc://apply-model-armor.js</Resource>
    <Resource>jsc://build-loki-payload.js</Resource>
  </Resources>
  <TargetEndpoints>
    <TargetEndpoint>mcp-onprem</TargetEndpoint>
  </TargetEndpoints>
</APIProxy>
```

---

### 5.2 Northbound ProxyEndpoint: `apiproxy/proxies/default.xml`

> **Note on Optional Loki Push**: Notice that `JS-BuildLokiPayload` and `SC-LokiAudit` in the `<PostFlow>` both have `continueOnError="true"` in their policy definitions. If you do not want Apigee to push Model Armor logs to Loki on port `3100`, you can either leave them as-is (they will never fail the request) or delete those two `<Step>` blocks from `<PostFlow>`, and Grafana will cleanly show `0` on the Apigee panels while continuing to display all MCP Server logs.

```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ProxyEndpoint name="default">
  <Description>Northbound MCP endpoint governed by API key, spike arrest, and Model Armor.</Description>
  <DefaultFaultRule name="default-fault">
    <Step>
      <Name>AM-FaultJson</Name>
    </Step>
    <AlwaysEnforce>false</AlwaysEnforce>
  </DefaultFaultRule>
  <PreFlow name="PreFlow">
    <Request>
      <Step>
        <Name>SA-SpikeArrest</Name>
      </Step>
      <Step>
        <Name>VA-VerifyApiKey</Name>
      </Step>
      <Step>
        <Name>AM-StripClientCredentials</Name>
      </Step>
      <Step>
        <Name>JS-CaptureRequest</Name>
      </Step>
    </Request>
    <Response/>
  </PreFlow>
  <Flows>
    <Flow name="mcp-jsonrpc">
      <Description>MCP JSON-RPC over HTTP POST (tools/list, tools/call, initialize, ping).</Description>
      <Request/>
      <Response/>
      <Condition>(proxy.pathsuffix MatchesPath "/mcp") and (request.verb = "POST")</Condition>
    </Flow>
    <Flow name="health">
      <Description>Liveness passthrough for smoke tests (requires API key).</Description>
      <Request/>
      <Response/>
      <Condition>(proxy.pathsuffix MatchesPath "/health") and (request.verb = "GET")</Condition>
    </Flow>
    <Flow name="not-found">
      <Description>Rejects any path/verb other than POST /mcp and GET /health.</Description>
      <Request>
        <Step>
          <Name>RF-NotFound</Name>
        </Step>
      </Request>
      <Response/>
    </Flow>
  </Flows>
  <PostFlow name="PostFlow">
    <Request/>
    <Response>
      <!-- OPTIONAL: Remove or keep these two steps. Both are best-effort (continueOnError=true). -->
      <Step>
        <Name>JS-BuildLokiPayload</Name>
        <Condition>hmcp.is_mcp = "true"</Condition>
      </Step>
      <Step>
        <Name>SC-LokiAudit</Name>
        <Condition>hmcp.is_mcp = "true"</Condition>
      </Step>
    </Response>
  </PostFlow>
  <HTTPProxyConnection>
    <BasePath>/telecom-mcp</BasePath>
  </HTTPProxyConnection>
  <RouteRule name="to-mcp-onprem">
    <TargetEndpoint>mcp-onprem</TargetEndpoint>
  </RouteRule>
</ProxyEndpoint>
```

---

### 5.3 Southbound TargetEndpoint: `apiproxy/targets/mcp-onprem.xml`

#### Option 1: With Model Armor Response Sanitization (Mode B)
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<TargetEndpoint name="mcp-onprem">
  <Description>On-Premises Ubuntu VM FastMCP server (Port 8001).</Description>
  <PreFlow name="PreFlow">
    <Request>
      <Step>
        <Name>AM-TargetIdentityHeaders</Name>
      </Step>
    </Request>
    <Response>
      <Step>
        <Name>JS-PrepareModelArmor</Name>
      </Step>
      <Step>
        <Name>SC-ModelArmor</Name>
        <Condition>hmcp.ma_needed = "true"</Condition>
      </Step>
      <Step>
        <Name>JS-ApplyModelArmor</Name>
      </Step>
    </Response>
  </PreFlow>
  <Flows/>
  <PostFlow name="PostFlow">
    <Request/>
    <Response/>
  </PostFlow>
  <HTTPTargetConnection>
    <Properties>
      <Property name="success.codes">1xx,2xx,3xx,4xx,5xx</Property>
      <Property name="io.timeout.millis">30000</Property>
      <Property name="connect.timeout.millis">5000</Property>
    </Properties>
    <!-- Replace 136.115.24.106 with your Ubuntu VM IP (do NOT append /mcp here) -->
    <URL>http://136.115.24.106:8001</URL>
  </HTTPTargetConnection>
</TargetEndpoint>
```

#### Option 2: Without Model Armor (Mode A — Pure Pass-Through to MCP Server)
If you want to test Apigee -> MCP Server connectivity without configuring Model Armor first, simply leave `<Response/>` empty in `apiproxy/targets/mcp-onprem.xml`:
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<TargetEndpoint name="mcp-onprem">
  <Description>On-Premises Ubuntu VM FastMCP server (Port 8001).</Description>
  <PreFlow name="PreFlow">
    <Request>
      <Step>
        <Name>AM-TargetIdentityHeaders</Name>
      </Step>
    </Request>
    <Response/>
  </PreFlow>
  <Flows/>
  <PostFlow name="PostFlow">
    <Request/>
    <Response/>
  </PostFlow>
  <HTTPTargetConnection>
    <Properties>
      <Property name="io.timeout.millis">30000</Property>
      <Property name="connect.timeout.millis">5000</Property>
    </Properties>
    <!-- Replace 136.115.24.106 with your Ubuntu VM IP (do NOT append /mcp here) -->
    <URL>http://136.115.24.106:8001</URL>
  </HTTPTargetConnection>
</TargetEndpoint>
```

---

### 5.4 Policy XML Files (`apiproxy/policies/*.xml`)

#### `apiproxy/policies/SA-SpikeArrest.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<SpikeArrest continueOnError="false" enabled="true" name="SA-SpikeArrest">
  <DisplayName>SA-SpikeArrest</DisplayName>
  <Rate>20ps</Rate>
  <UseEffectiveCount>true</UseEffectiveCount>
</SpikeArrest>
```

#### `apiproxy/policies/VA-VerifyApiKey.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<VerifyAPIKey continueOnError="false" enabled="true" name="VA-VerifyApiKey">
  <DisplayName>VA-VerifyApiKey</DisplayName>
  <APIKey ref="request.header.x-apikey"/>
</VerifyAPIKey>
```

#### `apiproxy/policies/AM-StripClientCredentials.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<AssignMessage continueOnError="false" enabled="true" name="AM-StripClientCredentials">
  <DisplayName>AM-StripClientCredentials</DisplayName>
  <Remove>
    <Headers>
      <Header name="x-apikey"/>
      <Header name="Authorization"/>
      <Header name="X-Serverless-Authorization"/>
    </Headers>
    <QueryParams>
      <QueryParam name="apikey"/>
    </QueryParams>
  </Remove>
  <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>
  <AssignTo createNew="false" transport="http" type="request"/>
</AssignMessage>
```

#### `apiproxy/policies/AM-TargetIdentityHeaders.xml`
Injects verified Apigee Developer App and Client ID headers into the southbound request to the Ubuntu VM MCP Server so they appear in Loki and Grafana:
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<AssignMessage continueOnError="false" enabled="true" name="AM-TargetIdentityHeaders">
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

#### `apiproxy/policies/JS-CaptureRequest.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Javascript continueOnError="false" enabled="true" timeLimit="200" name="JS-CaptureRequest">
  <DisplayName>JS-CaptureRequest</DisplayName>
  <ResourceURL>jsc://capture-request.js</ResourceURL>
</Javascript>
```

#### `apiproxy/policies/JS-PrepareModelArmor.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Javascript continueOnError="false" enabled="true" timeLimit="500" name="JS-PrepareModelArmor">
  <DisplayName>JS-PrepareModelArmor</DisplayName>
  <ResourceURL>jsc://prepare-model-armor.js</ResourceURL>
</Javascript>
```

#### `apiproxy/policies/SC-ModelArmor.xml`
Replace `<PROJECT_ID>`, `<LOCATION>` (e.g., `us-central1`), and `<TEMPLATE_ID>` (e.g., `telecom-mcp-armor`) with your Google Cloud Model Armor template details:
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ServiceCallout continueOnError="true" enabled="true" name="SC-ModelArmor">
  <DisplayName>SC-ModelArmor</DisplayName>
  <Request clearPayload="true" variable="maRequest">
    <IgnoreUnresolvedVariables>false</IgnoreUnresolvedVariables>
    <Set>
      <Headers>
        <Header name="Content-Type">application/json</Header>
      </Headers>
      <Payload contentType="application/json">{hmcp.ma_request_body}</Payload>
      <Verb>POST</Verb>
    </Set>
  </Request>
  <Response>maResponse</Response>
  <HTTPTargetConnection>
    <Authentication>
      <GoogleAccessToken>
        <Scopes>
          <Scope>https://www.googleapis.com/auth/cloud-platform</Scope>
        </Scopes>
      </GoogleAccessToken>
    </Authentication>
    <Properties>
      <Property name="io.timeout.millis">15000</Property>
      <Property name="connect.timeout.millis">5000</Property>
    </Properties>
    <URL>https://modelarmor.us-central1.rep.googleapis.com/v1/projects/<PROJECT_ID>/locations/us-central1/templates/telecom-mcp-armor:sanitizeModelResponse</URL>
  </HTTPTargetConnection>
</ServiceCallout>
```

#### `apiproxy/policies/JS-ApplyModelArmor.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Javascript continueOnError="false" enabled="true" timeLimit="1000" name="JS-ApplyModelArmor">
  <DisplayName>JS-ApplyModelArmor</DisplayName>
  <ResourceURL>jsc://apply-model-armor.js</ResourceURL>
</Javascript>
```

#### `apiproxy/policies/JS-BuildLokiPayload.xml` (Optional)
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Javascript continueOnError="true" enabled="true" timeLimit="500" name="JS-BuildLokiPayload">
  <DisplayName>JS-BuildLokiPayload</DisplayName>
  <Properties>
    <Property name="apigee_env">eval</Property>
  </Properties>
  <ResourceURL>jsc://build-loki-payload.js</ResourceURL>
</Javascript>
```

#### `apiproxy/policies/SC-LokiAudit.xml` (Optional)
Pushes Model Armor masking/blocking verdicts to Grafana Loki on your Ubuntu VM (`port 3100`). Because `continueOnError="true"` is set, if port `3100` is unreachable or disabled, Apigee ignores the failure and never interrupts agent traffic:
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
    <!-- Replace 136.115.24.106 with your Ubuntu VM IP -->
    <URL>http://136.115.24.106:3100/loki/api/v1/push</URL>
  </HTTPTargetConnection>
</ServiceCallout>
```

#### `apiproxy/policies/RF-NotFound.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RaiseFault continueOnError="false" enabled="true" name="RF-NotFound">
  <DisplayName>RF-NotFound</DisplayName>
  <FaultResponse>
    <Set>
      <Payload contentType="application/json">{"error":{"code":"NOT_FOUND","message":"Only POST /mcp and GET /health are exposed by this proxy."}}</Payload>
      <StatusCode>404</StatusCode>
      <ReasonPhrase>Not Found</ReasonPhrase>
    </Set>
  </FaultResponse>
  <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>
</RaiseFault>
```

#### `apiproxy/policies/AM-FaultJson.xml`
```xml
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<AssignMessage continueOnError="false" enabled="true" name="AM-FaultJson">
  <DisplayName>AM-FaultJson</DisplayName>
  <Set>
    <Payload contentType="application/json" variablePrefix="@" variableSuffix="#">{"error":{"code":"@fault.name#","status":@response.status.code#,"message":"Request rejected by the Apigee MCP gateway."}}</Payload>
  </Set>
  <IgnoreUnresolvedVariables>true</IgnoreUnresolvedVariables>
  <AssignTo createNew="false" transport="http" type="response"/>
</AssignMessage>
```

---

### 5.5 JavaScript Resource Files (`apiproxy/resources/jsc/*.js`)

#### `apiproxy/resources/jsc/capture-request.js`
```javascript
/*
 * Purpose: Captures JSON-RPC request metadata and caller identity for downstream audit policies.
 */
function headerValue(name) {
  var value = context.getVariable("request.header." + name);
  return value ? String(value).trim() : "";
}

function describeMessage(message) {
  var params = (message && message.params) || {};
  return {
    method: message && message.method ? String(message.method) : "",
    id: message && message.id !== undefined && message.id !== null ? String(message.id) : "",
    tool: params.name ? String(params.name) : ""
  };
}

var isMcp = context.getVariable("proxy.pathsuffix") === "/mcp" &&
  context.getVariable("request.verb") === "POST";
context.setVariable("hmcp.is_mcp", isMcp ? "true" : "false");
context.setVariable("hmcp.start_ms", String(Date.now()));

context.setVariable("hmcp.end_user_email",
  headerValue("x-end-user-email") || headerValue("x-user-email") || headerValue("x-goog-authenticated-user-email"));
var traceHeader = headerValue("x-cloud-trace-context") || headerValue("x-trace-id");
context.setVariable("hmcp.trace_id", traceHeader.split("/")[0]);

var methods = [];
var ids = [];
var tools = [];
if (isMcp) {
  try {
    var body = JSON.parse(context.getVariable("request.content") || "{}");
    var messages = Object.prototype.toString.call(body) === "[object Array]" ? body : [body];
    for (var i = 0; i < messages.length; i++) {
      var described = describeMessage(messages[i]);
      methods.push(described.method);
      ids.push(described.id);
      if (described.tool) {
        tools.push(described.tool);
      }
    }
  } catch (parseError) {
    context.setVariable("hmcp.request_parse_error", String(parseError));
  }
}
context.setVariable("hmcp.jsonrpc_method", methods.join(","));
context.setVariable("hmcp.jsonrpc_id", ids.join(","));
context.setVariable("hmcp.tool_name", tools.join(","));
```

#### `apiproxy/resources/jsc/prepare-model-armor.js`
```javascript
/*
 * Purpose: Collects every text segment of an MCP JSON-RPC response and builds one Model Armor request.
 */
var SEGMENT_SEPARATOR = "\n<<<HMCP_SEGMENT_BOUNDARY_7F3A9C>>>\n";

function collectSegments(message, out) {
  if (!message || typeof message !== "object") {
    return;
  }
  var result = message.result;
  if (result && Object.prototype.toString.call(result.content) === "[object Array]") {
    for (var i = 0; i < result.content.length; i++) {
      var part = result.content[i];
      if (part && part.type === "text" && typeof part.text === "string") {
        out.push(part.text);
      }
    }
  }
  if (message.error && typeof message.error.message === "string") {
    out.push(message.error.message);
  }
}

var isMcp = context.getVariable("hmcp.is_mcp") === "true";
var raw = context.getVariable("response.content") || "";
var statusCode = parseInt(context.getVariable("response.status.code"), 10);
var segments = [];
var kind = "empty";

context.setVariable("hmcp.ma_needed", "false");
context.setVariable("hmcp.action", "PASSED");

if (isMcp && raw.length > 0) {
  try {
    var parsed = JSON.parse(raw);
    var messages = Object.prototype.toString.call(parsed) === "[object Array]" ? parsed : [parsed];
    kind = messages === parsed ? "batch" : "single";
    for (var m = 0; m < messages.length; m++) {
      collectSegments(messages[m], segments);
    }
  } catch (parseError) {
    kind = "non_json";
    context.setVariable("hmcp.parse_error", String(parseError));
  }
}

context.setVariable("hmcp.response_kind", kind);
context.setVariable("hmcp.upstream_status", String(statusCode));
context.setVariable("hmcp.segment_count", String(segments.length));

if (segments.length > 0) {
  context.setVariable("hmcp.ma_needed", "true");
  context.setVariable("hmcp.ma_request_body", JSON.stringify({
    modelResponseData: { text: segments.join(SEGMENT_SEPARATOR) }
  }));
}
```

#### `apiproxy/resources/jsc/apply-model-armor.js`
```javascript
/*
 * Purpose: Applies the Model Armor verdict to the MCP JSON-RPC response before it leaves Apigee.
 */
var SEGMENT_SEPARATOR = "\n<<<HMCP_SEGMENT_BOUNDARY_7F3A9C>>>\n";
var BLOCKED_CODE = -32001;
var FAIL_CLOSED_CODE = -32002;
var MATCH_FOUND = "MATCH_FOUND";

function isArray(value) {
  return Object.prototype.toString.call(value) === "[object Array]";
}

function jsonRpcError(id, code, message) {
  return {
    jsonrpc: "2.0",
    id: id === undefined ? null : id,
    error: { code: code, message: message }
  };
}

function writeResponse(body, action, statusCode) {
  context.setVariable("response.content", JSON.stringify(body));
  context.setVariable("response.header.Content-Type", "application/json");
  if (statusCode) {
    context.setVariable("response.status.code", String(statusCode));
  }
  context.setVariable("hmcp.action", action);
}

function replaceAllWithError(messages, isBatch, code, reason, action) {
  if (!messages || messages.length === 0) {
    writeResponse(jsonRpcError(null, code, reason), action, 200);
    return;
  }
  var errors = [];
  for (var i = 0; i < messages.length; i++) {
    var original = messages[i] || {};
    errors.push(jsonRpcError(original.id, code, reason));
  }
  writeResponse(isBatch ? errors : errors[0], action, 200);
}

function applySegments(messages, segments) {
  var cursor = 0;
  for (var m = 0; m < messages.length; m++) {
    var message = messages[m];
    if (!message || typeof message !== "object") {
      continue;
    }
    var result = message.result;
    if (result && isArray(result.content)) {
      for (var i = 0; i < result.content.length; i++) {
        var part = result.content[i];
        if (part && part.type === "text" && typeof part.text === "string") {
          if (cursor >= segments.length) {
            return false;
          }
          part.text = segments[cursor];
          cursor++;
        }
      }
      if (result.structuredContent !== undefined) {
        delete result.structuredContent;
      }
    }
    if (message.error && typeof message.error.message === "string") {
      if (cursor >= segments.length) {
        return false;
      }
      message.error.message = segments[cursor];
      cursor++;
    }
  }
  return cursor === segments.length;
}

function stripStructuredContent(messages) {
  for (var m = 0; m < messages.length; m++) {
    var message = messages[m];
    if (message && message.result && message.result.structuredContent !== undefined) {
      delete message.result.structuredContent;
    }
  }
}

function readVerdicts(sanitizationResult) {
  var verdicts = { piMatch: false, uriMatch: false, sdpMatch: false, sdpText: null, sdpExecutionOk: true };
  var filterResults = sanitizationResult.filterResults || {};
  for (var key in filterResults) {
    if (!Object.prototype.hasOwnProperty.call(filterResults, key)) {
      continue;
    }
    var entry = filterResults[key] || {};
    if (entry.piAndJailbreakFilterResult && entry.piAndJailbreakFilterResult.matchState === MATCH_FOUND) {
      verdicts.piMatch = true;
    }
    if (entry.maliciousUriFilterResult && entry.maliciousUriFilterResult.matchState === MATCH_FOUND) {
      verdicts.uriMatch = true;
    }
    if (entry.sdpFilterResult && entry.sdpFilterResult.deidentifyResult) {
      var deidentify = entry.sdpFilterResult.deidentifyResult;
      if (deidentify.executionState && deidentify.executionState !== "EXECUTION_SUCCESS") {
        verdicts.sdpExecutionOk = false;
      }
      if (deidentify.matchState === MATCH_FOUND) {
        verdicts.sdpMatch = true;
        verdicts.sdpText = deidentify.data && typeof deidentify.data.text === "string" ? deidentify.data.text : null;
      }
    }
  }
  return verdicts;
}

function recordFindings(verdicts, invocation) {
  context.setVariable("hmcp.pi_match", String(verdicts.piMatch));
  context.setVariable("hmcp.uri_match", String(verdicts.uriMatch));
  context.setVariable("hmcp.sdp_match", String(verdicts.sdpMatch));
  context.setVariable("hmcp.ma_invocation", invocation || "");
}

function main() {
  context.setVariable("hmcp.pi_match", "false");
  context.setVariable("hmcp.uri_match", "false");
  context.setVariable("hmcp.sdp_match", "false");
  context.setVariable("hmcp.ma_invocation", "NOT_CALLED");

  if (context.getVariable("hmcp.is_mcp") !== "true") {
    return;
  }

  var kind = context.getVariable("hmcp.response_kind") || "empty";
  var upstreamStatus = parseInt(context.getVariable("hmcp.upstream_status"), 10) || 0;
  var raw = context.getVariable("response.content") || "";

  if (kind === "empty") {
    return;
  }

  if (kind === "non_json") {
    var reason = upstreamStatus >= 400 ?
      "Upstream MCP server error (details withheld by the Apigee MCP gateway)." :
      "Response withheld: non-JSON MCP payload cannot be sanitized (fail-closed).";
    writeResponse(jsonRpcError(null, FAIL_CLOSED_CODE, reason), "MA_ERROR", upstreamStatus >= 400 ? upstreamStatus : 502);
    return;
  }

  var parsed = JSON.parse(raw);
  var isBatch = isArray(parsed);
  var messages = isBatch ? parsed : [parsed];

  if (context.getVariable("hmcp.ma_needed") !== "true") {
    stripStructuredContent(messages);
    context.setVariable("response.content", JSON.stringify(parsed));
    return;
  }

  var calloutFailed = context.getVariable("servicecallout.SC-ModelArmor.failed") === true ||
    String(context.getVariable("servicecallout.SC-ModelArmor.failed")) === "true";
  var maStatus = parseInt(context.getVariable("maResponse.status.code"), 10) || 0;
  var maBody = context.getVariable("maResponse.content") || "";

  if (calloutFailed || maStatus !== 200 || maBody.length === 0) {
    context.setVariable("hmcp.ma_error", "callout_failed=" + calloutFailed + " status=" + maStatus);
    replaceAllWithError(messages, isBatch, FAIL_CLOSED_CODE,
      "Response withheld: Model Armor unavailable (fail-closed).", "MA_ERROR");
    return;
  }

  var sanitization;
  try {
    sanitization = (JSON.parse(maBody) || {}).sanitizationResult || {};
  } catch (maParseError) {
    context.setVariable("hmcp.ma_error", "unparseable Model Armor response: " + String(maParseError));
    replaceAllWithError(messages, isBatch, FAIL_CLOSED_CODE,
      "Response withheld: Model Armor unavailable (fail-closed).", "MA_ERROR");
    return;
  }

  var verdicts = readVerdicts(sanitization);
  recordFindings(verdicts, sanitization.invocationResult);

  if (sanitization.invocationResult !== "SUCCESS" || !verdicts.sdpExecutionOk) {
    context.setVariable("hmcp.ma_error", "invocationResult=" + sanitization.invocationResult);
    replaceAllWithError(messages, isBatch, FAIL_CLOSED_CODE,
      "Response withheld: Model Armor could not complete all filters (fail-closed).", "MA_ERROR");
    return;
  }

  if (verdicts.piMatch || verdicts.uriMatch) {
    var findings = [];
    if (verdicts.piMatch) {
      findings.push("prompt injection / jailbreak");
    }
    if (verdicts.uriMatch) {
      findings.push("malicious URI");
    }
    replaceAllWithError(messages, isBatch, BLOCKED_CODE,
      "Blocked by Model Armor: tool result contained " + findings.join(" and ") + ".", "BLOCKED");
    return;
  }

  if (verdicts.sdpMatch) {
    if (verdicts.sdpText === null) {
      replaceAllWithError(messages, isBatch, FAIL_CLOSED_CODE,
        "Response withheld: Model Armor returned no de-identified text (fail-closed).", "MA_ERROR");
      return;
    }
    var segments = verdicts.sdpText.split(SEGMENT_SEPARATOR);
    var expected = parseInt(context.getVariable("hmcp.segment_count"), 10) || 0;
    if (segments.length !== expected || !applySegments(messages, segments)) {
      context.setVariable("hmcp.ma_error", "segment mismatch expected=" + expected + " got=" + segments.length);
      replaceAllWithError(messages, isBatch, FAIL_CLOSED_CODE,
        "Response withheld: de-identified text could not be mapped back (fail-closed).", "MA_ERROR");
      return;
    }
    context.setVariable("hmcp.masked_text", verdicts.sdpText);
    context.setVariable("response.content", JSON.stringify(parsed));
    context.setVariable("hmcp.action", "MASKED");
    return;
  }

  stripStructuredContent(messages);
  try {
    context.setVariable("hmcp.masked_text",
      JSON.parse(context.getVariable("hmcp.ma_request_body")).modelResponseData.text);
  } catch (auditError) {
    context.setVariable("hmcp.audit_error", String(auditError));
  }
  context.setVariable("response.content", JSON.stringify(parsed));
  context.setVariable("hmcp.action", "PASSED");
}

try {
  main();
} catch (unexpected) {
  context.setVariable("hmcp.ma_error", "apply-model-armor exception: " + String(unexpected));
  writeResponse(jsonRpcError(null, FAIL_CLOSED_CODE,
    "Response withheld: sanitization error in the Apigee MCP gateway (fail-closed)."), "MA_ERROR", 502);
}
```

#### `apiproxy/resources/jsc/build-loki-payload.js` (Optional)
```javascript
/*
 * Purpose: Builds the Loki push payload that records the Model Armor decision for one MCP response.
 */
var MAX_TEXT_CHARS = 8000;

function readVar(name) {
  var value = context.getVariable(name);
  return value === null || value === undefined ? "" : String(value);
}

function truncate(text, limit) {
  if (text.length <= limit) {
    return text;
  }
  return text.substring(0, limit) + "...[truncated " + (text.length - limit) + " chars]";
}

var action = readVar("hmcp.action") || "PASSED";
var startMs = parseInt(readVar("hmcp.start_ms"), 10) || Date.now();
var developerApp = readVar("verifyapikey.VA-VerifyApiKey.app.name") || readVar("developer.app.name");

var line = {
  event_type: "APIGEE_MODEL_ARMOR",
  action: action,
  tool_name: readVar("hmcp.tool_name"),
  jsonrpc_method: readVar("hmcp.jsonrpc_method"),
  jsonrpc_id: readVar("hmcp.jsonrpc_id"),
  developer_app: developerApp,
  client_id: readVar("verifyapikey.VA-VerifyApiKey.client_id") || readVar("client_id"),
  end_user_email: readVar("hmcp.end_user_email"),
  trace_id: readVar("hmcp.trace_id"),
  upstream_status: readVar("hmcp.upstream_status"),
  pi_jailbreak_match: readVar("hmcp.pi_match") === "true",
  malicious_uri_match: readVar("hmcp.uri_match") === "true",
  sdp_match: readVar("hmcp.sdp_match") === "true",
  ma_invocation: readVar("hmcp.ma_invocation"),
  ma_error: readVar("hmcp.ma_error"),
  latency_ms: Date.now() - startMs,
  masked_response: truncate(readVar("hmcp.masked_text"), MAX_TEXT_CHARS)
};

var timestampNs = String(Date.now()) + "000000";

context.setVariable("hmcp.loki_payload", JSON.stringify({
  streams: [{
    stream: {
      app: "apigee-mcp-proxy",
      apigee_env: String(properties.apigee_env || "unknown"),
      action: action
    },
    values: [[timestampNs, JSON.stringify(line)]]
  }]
}));
```

---

## 6. Creating the API Product, Developer, and Developer App in Apigee

Because `VA-VerifyApiKey` checks `x-apikey` on every incoming request, create the following three items in the Apigee UI (or via REST API):

1. **API Product (`Publish -> API Products -> + Create`)**:
   - **Name**: `telecom-mcp-product`
   - **Environment**: Select your deployed environment (e.g., `eval` or `prod`)
   - **Access**: `Private`
   - **Operation / Proxy**: Add proxy `telecom-mcp` with path `/**` (or `/mcp` and `/health`).
2. **Developer (`Publish -> Developers -> + Create`)**:
   - **Email**: `agent-developer@telecom.example`
   - **First / Last Name**: `Telecom` / `AI Agent`
3. **Developer App (`Publish -> Apps -> + Create`)**:
   - **Name**: `telecom-adk-agent-app`
   - **Developer**: `agent-developer@telecom.example`
   - **Credentials / Product**: Add `telecom-mcp-product`
   - Copy the generated **Key (`Consumer Key`)** — this is the value you pass in the `x-apikey` header when calling Apigee!

*(Note: If your proxy uses `SC-ModelArmor`, deploy the proxy revision in Apigee with a Google Service Account that has the `roles/modelarmor.user` IAM role.)*

---

## 7. Step-by-Step Verification & Smoke Tests

Once your `telecom-mcp` proxy is deployed in Apigee, run these 4 `curl` commands to verify the entire flow end-to-end:

### Test 1: Health Check Through Apigee (`GET /health`)
```bash
export APIGEE_HOST="<YOUR_APIGEE_HOSTNAME>"
export APIGEE_KEY="<YOUR_APIGEE_CONSUMER_KEY>"

curl -i -s "https://${APIGEE_HOST}/telecom-mcp/health" \
  -H "x-apikey: ${APIGEE_KEY}"
```
**Expected Output**: HTTP `200 OK` with `{"status":"healthy","service":"telecom-mcp-server","version":"2.0.0"}`.

### Test 2: Verify API Key Rejection (`401 Unauthorized`)
```bash
curl -i -s "https://${APIGEE_HOST}/telecom-mcp/mcp" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```
**Expected Output**: HTTP `401 Unauthorized` (`{"error":{"code":"steps.oauth.v2.FailedToResolveAPIKey","status":401,"message":"Request rejected by the Apigee MCP gateway."}}`).

### Test 3: Call `get_customer_360` Through Apigee (PII Masking Verification)
```bash
curl -s -X POST "https://${APIGEE_HOST}/telecom-mcp/mcp" \
  -H "Content-Type: application/json" \
  -H "x-apikey: ${APIGEE_KEY}" \
  -H "x-end-user-email: sarah.jenkins@telecom.example" \
  -d '{
    "jsonrpc": "2.0",
    "id": 101,
    "method": "tools/call",
    "params": {
      "name": "get_customer_360",
      "arguments": {
        "phone_number": "+1 (555) 234-5678"
      }
    }
  }' | jq .
```
- **In Mode A (Pass-Through)**: Returns Elena Rostova's full Customer 360 JSON from the Ubuntu VM PostgreSQL database.
- **In Mode B (With Model Armor)**: Returns Elena Rostova's Customer 360 JSON with `subscriber_name`, `email`, `ssn`, `street_address`, and `payment_card` replaced by `[PERSON_NAME]`, `[EMAIL_ADDRESS]`, `[US_SOCIAL_SECURITY_NUMBER]`, `[STREET_ADDRESS]`, and `[CREDIT_CARD_NUMBER]` while keeping `phone_number` (`+15552345678`) intact!

### Test 4: Call `get_customer_360` on Security Test Persona (Prompt Injection Blocking in Mode B)
```bash
curl -s -X POST "https://${APIGEE_HOST}/telecom-mcp/mcp" \
  -H "Content-Type: application/json" \
  -H "x-apikey: ${APIGEE_KEY}" \
  -H "x-end-user-email: sarah.jenkins@telecom.example" \
  -d '{
    "jsonrpc": "2.0",
    "id": 102,
    "method": "tools/call",
    "params": {
      "name": "get_customer_360",
      "arguments": {
        "phone_number": "+1 (555) 000-1337"
      }
    }
  }' | jq .
```
- **In Mode B (With Model Armor)**: Model Armor detects the prompt injection and malicious URL inside Mallory Tester's ticket and returns JSON-RPC error `-32001`:
  ```json
  {
    "jsonrpc": "2.0",
    "id": 102,
    "error": {
      "code": -32001,
      "message": "Blocked by Model Armor: tool result contained prompt injection / jailbreak and malicious URI."
    }
  }
  ```

---

## 8. Common Pitfalls Checklist (Foolproof Troubleshooting)

1. **Do NOT append `/mcp` to the TargetEndpoint `<URL>`**:
   - **Correct**: `<URL>http://136.115.24.106:8001</URL>`
   - **Incorrect**: `<URL>http://136.115.24.106:8001/mcp</URL>` (Apigee automatically appends `/mcp` from the incoming request path; adding `/mcp` to `<URL>` would result in `http://136.115.24.106:8001/mcp/mcp` -> HTTP `404`).
2. **Do NOT use `<GoogleIDToken>` in `mcp-onprem.xml` unless desired for audit**:
   - The Ubuntu VM MCP server does not require a Cloud Run IAM token. Using plain `<HTTPTargetConnection><URL>http://<UBUNTU_VM_IP>:8001</URL></HTTPTargetConnection>` works out-of-the-box without needing `roles/iam.serviceAccountTokenCreator`.
3. **Loki Audit Push (`SC-LokiAudit`) is 100% Optional**:
   - Even if Apigee cannot reach `http://<UBUNTU_VM_IP>:3100/loki/api/v1/push` (or you remove `SC-LokiAudit` from `default.xml`), neither Apigee nor Grafana will throw any errors. The MCP Server on the Ubuntu VM always logs all tool executions and Apigee headers (`X-Apigee-Developer-App`, `X-Apigee-Client-Id`, `X-Apigee-Proxy`) to its local Loki instance automatically.
