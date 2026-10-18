"""
Purpose: FastMCP server implementing MCP over HTTP (JSON-RPC 2.0), Server-Sent Events discovery,
         a direct /execute bridge, health and Prometheus metrics endpoints.
Architecture/Context: Runs on Cloud Run behind IAM (no allUsers). Only two principals hold
                      roles/run.invoker: the Apigee proxy service account (which calls /mcp with a
                      Google ID token on behalf of the ADK agent that came through Agent Gateway)
                      and the Prometheus service account (which scrapes /metrics). Cloud Run IAM is
                      therefore the ONLY authentication gate - there is no application token check.
                      The server returns RAW tool results; the Apigee proxy runs Model Armor + SDP
                      on every response to mask PII ([PERSON_NAME], [EMAIL_ADDRESS], ...) or block
                      prompt-injection content (JSON-RPC error -32001) before it reaches the agent.
Dependencies/Side Effects: Initializes the database schema and seeds synthetic data at startup,
                           streams full request/response audit records to Loki (restricted PII
                           store), and exposes Prometheus metrics.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from src.core.audit import audit_client
from src.core.config import settings
from src.core.security import (
    DEFAULT_AGENT_CALLER,
    decode_jwt_unverified,
    extract_security_context,
)
from src.db.database import init_db
from src.db.seed_data import seed_synthetic_telecom_data
from src.mcp.tools import (
    TOOL_CALL_COUNTER,
    check_network_outages_tool,
    get_billing_breakdown_tool,
    get_customer_360_tool,
    get_service_diagnostics_tool,
    get_upsell_recommendations_tool,
    log_agent_interaction_tool,
    run_remote_device_action_tool,
    search_customer_tool,
)

logger = logging.getLogger("hybrid_mcp.mcp_server")

SERVER_NAME = "telecom-mcp-server"
SERVER_VERSION = "2.0.0"
MCP_PROTOCOL_VERSION = "2024-11-05"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Summary:
        Handles startup and shutdown lifecycle events for the MCP server.
        Initializes database tables, seeds synthetic records (advisory-locked on PostgreSQL),
        and primes baseline Prometheus metric series for instant dashboard visibility.

    Parameters:
        app (FastAPI): The application instance (required by the lifespan protocol).

    Return Value:
        AsyncIterator[None]: Yields once while the application is serving.

    Exceptions/Errors:
        DatabaseConfigurationError: Propagated from init_db() when PostgreSQL is unreachable and
        DB_ALLOW_SQLITE_FALLBACK is false (the container then fails fast and Cloud Run restarts it).
    """
    logger.info("Initializing MCP Server database and synthetic records...")
    await init_db()
    await seed_synthetic_telecom_data()

    # Prime a zero-valued series so Grafana panels render immediately on cold boots.
    TOOL_CALL_COUNTER.labels(tool_name="system_ready", status="SUCCESS").inc(0)

    yield
    logger.info("Shutting down MCP Server resources...")
    await audit_client.close()


app = FastAPI(
    title="Telecom FastMCP Server (behind Apigee + Model Armor)",
    description=(
        "MCP server exposing telecom customer-care tools. Protected by Cloud Run IAM; responses are "
        "masked/screened by the Apigee + Model Armor security gateway before reaching the AI agent."
    ),
    version=SERVER_VERSION,
    lifespan=lifespan
)


async def build_audit_context(request: Request) -> Dict[str, Any]:
    """
    Summary:
        FastAPI dependency that describes the caller for the audit trail. It NEVER rejects a
        request: authentication has already been enforced by Cloud Run IAM before the request
        reached the container. The context is also stored on request.state for reuse.

    Parameters:
        request (Request): Incoming FastAPI request.

    Return Value:
        Dict[str, Any]: {'security_context': dict, 'jwt_claims': dict|None,
        'headers': dict, 'caller': str}.

    Exceptions/Errors:
        None.
    """
    raw_headers = dict(request.headers)
    token_candidate = raw_headers.get("authorization") or raw_headers.get("x-serverless-authorization")
    security_context = extract_security_context(
        token=token_candidate,
        headers=raw_headers,
        client_ip=request.client.host if request.client else None
    )
    request.state.security_context = security_context
    return {
        "security_context": security_context,
        "jwt_claims": decode_jwt_unverified(token_candidate),
        "headers": raw_headers,
        "caller": security_context.get("caller_identity", DEFAULT_AGENT_CALLER),
    }


# PII disclaimer shared by tool descriptions: tells the agent (and reviewers) where masking happens.
_GATEWAY_PII_NOTE = (
    "Personal data (names, emails, SSNs, street addresses, payment cards) is masked by the Apigee + "
    "Model Armor security gateway before it reaches the AI agent; the phone number stays visible as "
    "the subscriber identity."
)

# Tool Catalog Definition (MCP Specification standard)
# Note: Agent identity is extracted automatically from request headers and JWT tokens,
# NOT requested as a tool parameter from the user.
TOOL_DEFINITIONS = [
    {
        "name": "search_customer",
        "description": "Finds subscriber records by Mobile or Landline phone number (or account query) and lists open ticket titles. " + _GATEWAY_PII_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number (e.g. '+1 (555) 234-5678' or digits '5552345678'). Primary unique identity."},
                "query": {"type": "string", "description": "Alternative search keyword: phone number, account ID, or general search."}
            }
        }
    },
    {
        "name": "get_customer_360",
        "description": "Returns consolidated subscriber profile (active broadband/mobile subscriptions, equipment, recent bills, and tickets) driven by Mobile or Landline phone number. " + _GATEWAY_PII_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number used as primary identity."},
                "customer_id": {"type": "string", "description": "Internal Customer UUID (optional if phone_number is supplied)."}
            }
        }
    },
    {
        "name": "get_service_diagnostics",
        "description": "Runs remote line diagnostics for Home Broadband ONT or Mobile 5G lines. Accepts subscriber phone number or subscription UUID. Returns dBm signal, packet loss, and recommended fix.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number to automatically diagnose their active service."},
                "service_id": {"type": "string", "description": "Subscription UUID (optional if phone_number is supplied)."}
            }
        }
    },
    {
        "name": "run_remote_device_action",
        "description": "Executes remote maintenance commands on customer hardware: 'reboot', 'channel_optimization', or 'ping_sweep'. Accepts subscriber phone number or device UUID. Requires explicit customer consent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number to locate and reboot premise equipment."},
                "device_id": {"type": "string", "description": "Hardware device UUID (optional if phone_number is supplied)."},
                "action": {"type": "string", "enum": ["reboot", "channel_optimization", "ping_sweep"], "description": "Action to execute"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "check_network_outages",
        "description": "Checks for ongoing infrastructure fiber cuts or 5G cell tower maintenance in a given postal area or for a subscriber's phone number.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number to resolve their service area."},
                "postal_code": {"type": "string", "description": "Customer 5-digit postal code (optional if phone_number is supplied)."}
            }
        }
    },
    {
        "name": "get_billing_breakdown",
        "description": "Returns itemized invoice records, roaming fees, dispute notes and payment status. Accepts subscriber phone number or account ID. Payment card numbers are masked by the Apigee + Model Armor security gateway before they reach the AI agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number."},
                "account_id": {"type": "string", "description": "Account UUID or Account Number (optional if phone_number is supplied)."}
            }
        }
    },
    {
        "name": "get_upsell_recommendations",
        "description": "Computes personalized upgrade offers (Gigabit fiber, Unlimited 5G pass, Roaming bundle) and provides agent pitch scripts. Identified by subscriber phone number or UUID. " + _GATEWAY_PII_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number."},
                "customer_id": {"type": "string", "description": "Customer UUID (optional if phone_number is supplied)."}
            }
        }
    },
    {
        "name": "log_agent_interaction",
        "description": "Logs agent call notes, resolution status, and upsell outcome directly into the CRM database and audit trail. Identified by subscriber phone number or UUID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "Subscriber Mobile or Landline phone number."},
                "customer_id": {"type": "string", "description": "Customer UUID (optional if phone_number is supplied)."},
                "agent_name": {"type": "string", "description": "Agent name or ID"},
                "issue_summary": {"type": "string", "description": "Problem described by customer"},
                "resolution_summary": {"type": "string", "description": "Solution provided"},
                "call_duration_sec": {"type": "integer", "description": "Call duration in seconds"},
                "upsell_offered": {"type": "boolean", "description": "Was an upsell offer pitched"},
                "upsell_accepted": {"type": "boolean", "description": "Did the customer accept"}
            },
            "required": ["issue_summary", "resolution_summary"]
        }
    }
]


# Dispatch table
TOOL_HANDLER_MAP = {
    "search_customer": search_customer_tool,
    "get_customer_360": get_customer_360_tool,
    "get_service_diagnostics": get_service_diagnostics_tool,
    "run_remote_device_action": run_remote_device_action_tool,
    "check_network_outages": check_network_outages_tool,
    "get_billing_breakdown": get_billing_breakdown_tool,
    "get_upsell_recommendations": get_upsell_recommendations_tool,
    "log_agent_interaction": log_agent_interaction_tool,
}


async def _read_json_or_400(request: Request, audit: Dict[str, Any]) -> Any:
    """
    Summary:
        Parses the JSON body; on failure audits the raw body and raises HTTP 400.

    Parameters:
        request (Request): Incoming request.
        audit (Dict[str, Any]): Context from build_audit_context.

    Return Value:
        Any: Parsed JSON (object or array).

    Exceptions/Errors:
        HTTPException(400): If the body is not valid JSON.
    """
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raw_text = (await request.body()).decode("utf-8", errors="replace")
        logger.warning("Rejected request with invalid JSON body on %s: %s", request.url.path, exc)
        await audit_client.log_http_request(
            endpoint=request.url.path,
            method=request.method,
            headers=audit["headers"],
            body=raw_text,
            caller_identity=audit["caller"],
            security_context=audit["security_context"],
            jwt_claims=audit["jwt_claims"],
            status="INVALID_JSON"
        )
        raise HTTPException(status_code=400, detail="Invalid JSON in request body.") from exc


@app.get("/health")
async def health_check():
    """
    Summary:
        Health check endpoint for Cloud Run startup/liveness probes and local orchestration.

    Parameters:
        None.

    Return Value:
        Dict[str, str]: Static health payload.

    Exceptions/Errors:
        None.
    """
    return {"status": "healthy", "service": SERVER_NAME, "version": SERVER_VERSION}


@app.get("/metrics")
async def metrics():
    """
    Summary:
        Exposes Prometheus metrics for scraping (Cloud Run IAM restricts this to the Prometheus SA).

    Parameters:
        None.

    Return Value:
        Response: Prometheus text exposition format.

    Exceptions/Errors:
        None.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/tools")
async def list_tools(request: Request, audit: Dict[str, Any] = Depends(build_audit_context)):
    """
    Summary:
        Returns the list of available MCP tools and their JSON schemas (non-MCP convenience route).

    Parameters:
        request (Request): Incoming request.
        audit (Dict[str, Any]): Audit context from build_audit_context.

    Return Value:
        Dict[str, Any]: {'tools': TOOL_DEFINITIONS}.

    Exceptions/Errors:
        None.
    """
    await audit_client.log_http_request(
        endpoint=request.url.path,
        method=request.method,
        headers=audit["headers"],
        body={"action": "list_tools", "count": len(TOOL_DEFINITIONS)},
        caller_identity=audit["caller"],
        security_context=audit["security_context"],
        jwt_claims=audit["jwt_claims"],
        status="LISTED"
    )
    return {"tools": TOOL_DEFINITIONS}


@app.post("/execute")
async def execute_tool(request: Request, audit: Dict[str, Any] = Depends(build_audit_context)):
    """
    Summary:
        Direct tool execution endpoint ({'name': ..., 'arguments': {...}}) for HTTP bridges and tests.

    Parameters:
        request (Request): Incoming request.
        audit (Dict[str, Any]): Audit context from build_audit_context.

    Return Value:
        Dict[str, Any]: {'result': <raw tool result>}.

    Exceptions/Errors:
        HTTPException(400): Invalid JSON, non-object body, or unknown tool name.
    """
    body = await _read_json_or_400(request, audit)
    security_context = audit["security_context"]

    await audit_client.log_http_request(
        endpoint=request.url.path,
        method=request.method,
        headers=audit["headers"],
        body=body,
        caller_identity=audit["caller"],
        security_context=security_context,
        jwt_claims=audit["jwt_claims"],
        status="RECEIVED"
    )

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    tool_name = body.get("name")
    arguments = body.get("arguments", {}) or {}
    if not isinstance(arguments, dict):
        raise HTTPException(status_code=400, detail="'arguments' must be a JSON object.")
    if not tool_name or tool_name not in TOOL_HANDLER_MAP:
        raise HTTPException(status_code=400, detail=f"Tool '{tool_name}' is not recognized.")

    explicit_user = arguments.get("agent_email")
    if isinstance(explicit_user, str) and "@" in explicit_user:
        security_context["end_user_email"] = explicit_user.strip()

    # caller_id/security_context are server-controlled; drop client-supplied duplicates.
    safe_args = {k: v for k, v in arguments.items() if k not in ("caller_id", "security_context")}
    handler = TOOL_HANDLER_MAP[tool_name]
    result = await handler(**safe_args, caller_id=audit["caller"], security_context=security_context)
    return {"result": result}


async def _process_single_mcp_message(
    payload: Dict[str, Any],
    caller_id: str = DEFAULT_AGENT_CALLER,
    security_context: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Summary:
        Processes a single JSON-RPC 2.0 MCP message according to the Model Context Protocol.

    Parameters:
        payload (Dict[str, Any]): Parsed JSON-RPC request dictionary.
        caller_id (str): Identifier of the caller for audit logging.
        security_context (Optional[Dict[str, Any]]): Security and network context for compliance.

    Return Value:
        Optional[Dict[str, Any]]: JSON-RPC response object, or None for notifications.
        Tool results are returned RAW in result.content[0].text (Apigee masks them downstream).

    Exceptions/Errors:
        None raised; tool failures are converted to JSON-RPC error -32000.
    """
    msg_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params", {}) or {}

    # 1. MCP Lifecycle: initialize
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False}
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION
                }
            }
        }

    # 2. MCP Lifecycle: notifications/initialized
    elif method == "notifications/initialized":
        if msg_id is not None:
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        return None

    # 3. MCP Ping
    elif method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {}
        }

    # 4. MCP Tools List
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": TOOL_DEFINITIONS}
        }

    # 5. MCP Tools Call
    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {}) or {}

        if tool_name not in TOOL_HANDLER_MAP:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {tool_name}"}
            }
        if not isinstance(tool_args, dict):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "Invalid params: 'arguments' must be an object."}
            }

        # Check for human operator metadata in arguments or MCP _meta payload
        meta = params.get("_meta", {}) or payload.get("_meta", {}) or {}
        explicit_user = (
            meta.get("user")
            or meta.get("user_email")
            or meta.get("agent_email")
            or tool_args.get("agent_email")
            or tool_args.get("caller_user")
        )
        if explicit_user and isinstance(explicit_user, str) and explicit_user.strip():
            if security_context is not None:
                security_context["end_user_email"] = explicit_user.strip()

        # The direct caller is strictly the AI Agent invoking this tool
        agent_caller = security_context.get("caller", caller_id) if security_context else caller_id
        # caller_id/security_context are server-controlled; drop client-supplied duplicates.
        safe_args = {k: v for k, v in tool_args.items() if k not in ("caller_id", "security_context")}

        handler = TOOL_HANDLER_MAP[tool_name]
        try:
            tool_result = await handler(
                **safe_args,
                caller_id=agent_caller,
                security_context=security_context
            )
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(tool_result, default=str)
                        }
                    ]
                }
            }
        except Exception as err:  # noqa: BLE001 - any tool failure must become a JSON-RPC error
            logger.error("Error executing tool %s: %s", tool_name, str(err), exc_info=True)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": f"Execution error: {str(err)}"}
            }

    # Fallback for unrecognized method
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method '{method}' is not supported."}
    }


@app.get("/mcp")
@app.get("/sse")
@app.get("/")
async def mcp_sse_stream(request: Request, audit: Dict[str, Any] = Depends(build_audit_context)):
    """
    Summary:
        Server-Sent Events (SSE) and HTTP discovery endpoint implementing MCP connectivity.
        Streams text/event-stream for SSE clients and returns a JSON tool listing otherwise.

    Parameters:
        request (Request): Incoming request.
        audit (Dict[str, Any]): Audit context from build_audit_context.

    Return Value:
        Dict[str, Any] | StreamingResponse: Discovery JSON or SSE stream.

    Exceptions/Errors:
        None.
    """
    await audit_client.log_http_request(
        endpoint=request.url.path,
        method=request.method,
        headers=audit["headers"],
        body={"action": "sse_connect", "accept": request.headers.get("accept", "")},
        caller_identity=audit["caller"],
        security_context=audit["security_context"],
        jwt_claims=audit["jwt_claims"],
        status="CONNECTED"
    )

    accept_header = request.headers.get("accept", "")
    if "text/event-stream" not in accept_header:
        return {
            "status": "healthy",
            "service": SERVER_NAME,
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "tools": TOOL_DEFINITIONS
        }

    async def event_generator():
        """
        Summary:
            Emits the MCP endpoint handshake followed by periodic keep-alive comments.

        Parameters:
            None.

        Return Value:
            AsyncIterator[str]: SSE-formatted chunks until the client disconnects.

        Exceptions/Errors:
            None.
        """
        handshake_data = {
            "type": "endpoint",
            "endpoint": "/mcp"
        }
        yield f"event: endpoint\ndata: {json.dumps(handshake_data)}\n\n"

        # Heartbeats keep intermediaries (Apigee, Google front end) from idling out the stream.
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(15)
            yield f": ping {json.dumps({'timestamp': asyncio.get_event_loop().time()})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/mcp")
@app.post("/messages")
@app.post("/")
async def handle_mcp_message(request: Request, audit: Dict[str, Any] = Depends(build_audit_context)):
    """
    Summary:
        MCP JSON-RPC message endpoint. Dispatches initialize, ping, tools/list, or tools/call commands.
        Handles both individual and batch JSON-RPC payloads from the Apigee-fronted ADK agent.
        Logs every incoming request header and message body payload to Loki for audit and security analysis.

    Parameters:
        request (Request): Incoming request.
        audit (Dict[str, Any]): Audit context from build_audit_context.

    Return Value:
        Dict | List | Response: JSON-RPC response(s), or HTTP 204 for pure notifications.

    Exceptions/Errors:
        HTTPException(400): Invalid JSON or a body that is neither an object nor an array.
    """
    payload = await _read_json_or_400(request, audit)
    security_context = audit["security_context"]

    # Log complete headers and body of the message to Loki
    await audit_client.log_http_request(
        endpoint=request.url.path,
        method=request.method,
        headers=audit["headers"],
        body=payload,
        caller_identity=audit["caller"],
        security_context=security_context,
        jwt_claims=audit["jwt_claims"],
        status="RECEIVED"
    )

    if isinstance(payload, list):
        responses = []
        for item in payload:
            if isinstance(item, dict):
                res = await _process_single_mcp_message(
                    item,
                    caller_id=audit["caller"],
                    security_context=security_context
                )
                if res is not None:
                    responses.append(res)
        return responses

    elif isinstance(payload, dict):
        res = await _process_single_mcp_message(
            payload,
            caller_id=audit["caller"],
            security_context=security_context
        )
        if res is None:
            return Response(status_code=204)
        return res

    raise HTTPException(status_code=400, detail="Request body must be a JSON object or array.")


def run():
    """
    Summary:
        CLI runner for launching the MCP server standalone.
        Dynamically respects the PORT environment variable assigned by Cloud Run or container orchestrators.
        Suppresses repetitive 10-second /health and /metrics probe lines from uvicorn.access so container
        stdout logs only record real MCP / tool traffic.

    Parameters:
        None.

    Return Value:
        None (blocks while serving).

    Exceptions/Errors:
        ValueError: If PORT is not an integer.
    """
    import uvicorn

    class _ProbeAccessLogFilter(logging.Filter):
        """Filters out repetitive /health and /metrics 200 OK probe lines from uvicorn.access."""

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return "GET /health " not in msg and "GET /metrics " not in msg

    logging.getLogger("uvicorn.access").addFilter(_ProbeAccessLogFilter())
    port = int(os.environ.get("PORT", settings.PORT or settings.MCP_PORT))
    # proxy_headers lets uvicorn honour X-Forwarded-* from the Google front end for audit IPs.
    uvicorn.run("src.mcp.server:app", host="0.0.0.0", port=port, reload=False, proxy_headers=True,
                forwarded_allow_ips="*")


if __name__ == "__main__":
    run()
