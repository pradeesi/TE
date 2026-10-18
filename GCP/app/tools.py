"""
Purpose: Telecom domain tool integrations bridging the Google ADK Agent on GCP Agent Runtime
         to the On-Premises MCP Server via an Apigee API Proxy over Cloud VPN.
Architecture/Context: Dispatches tool calls over HTTP JSON-RPC 2.0 (`POST /mcp`) to the Apigee proxy.
                      Supports flexible Apigee authentication (API Key, Bearer Token, Google OIDC ID Token,
                      HTTP Basic Auth, Custom Header, or Unauthenticated/mTLS over Cloud VPN) and emits
                      structured telemetry logs correlated with Google Cloud Trace and Cloud Logging.
Dependencies/Side Effects: Interacts with Google Auth / Metadata Server, httpx for async HTTP dispatch,
                           and Google ADK ToolContext for caller identity and distributed trace propagation.
"""

import base64
import json
import logging
import subprocess
import time
import urllib.request
from typing import Any, Dict, Optional

import httpx
from google.adk.tools import ToolContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from app.config import agent_settings

logger = logging.getLogger("telecom_agent.tools")

# JSON-RPC error codes emitted when Apigee + Model Armor security policies are active
MODEL_ARMOR_BLOCK_CODE = -32001
SECURITY_GATEWAY_UNAVAILABLE_CODE = -32002

# In-memory cached IAM OIDC token for Google ID token authentication mode
_cached_token = ""
_token_expiry = 0.0


def _emit_agent_runtime_log(
    level: int,
    event: str,
    trace_id: Optional[str] = None,
    **fields: Any,
) -> None:
    """
    Summary:
        Emits a structured JSON log entry compatible with GCP Agent Runtime (Vertex AI
        Reasoning Engine), Cloud Logging, and Cloud Trace correlation.

    Parameters:
        level (int): Python logging severity level (e.g., logging.INFO, logging.ERROR).
        event (str): Machine-readable event identifier (e.g., 'mcp_tool_call_completed').
        trace_id (Optional[str]): Distributed trace or invocation ID for Cloud Trace correlation.
        **fields (Any): Additional structured metadata fields (never include raw secrets).

    Return Value:
        None.

    Exceptions/Errors:
        None (serialization fallbacks ensure logging never interrupts tool execution).
    """
    project_id = agent_settings.GOOGLE_CLOUD_PROJECT
    payload: Dict[str, Any] = {
        "event": event,
        "service_name": "telecom-agent",
        "agent_name": agent_settings.AGENT_NAME,
        "agent_identity": agent_settings.AGENT_IDENTITY,
        **fields,
    }
    if trace_id and project_id:
        payload["logging.googleapis.com/trace"] = f"projects/{project_id}/traces/{trace_id}"

    logger.log(
        level,
        json.dumps(payload, default=str, sort_keys=True),
        extra={"json_fields": payload},
    )


def _get_cloud_run_id_token(target_audience: str) -> str:
    """
    Summary:
        Retrieves a valid Google Cloud Identity (OIDC) ID token for authenticating
        to Apigee or Google Cloud Run endpoints when APIGEE_AUTH_MODE='google_id_token'.
        Uses the GCP instance metadata server on Agent Runtime and falls back to gcloud CLI locally.

    Parameters:
        target_audience (str): Target audience URI for the OIDC ID token.

    Return Value:
        str: Valid Google OIDC bearer token string, or empty string if unavailable.

    Exceptions/Errors:
        None (suppresses network/subprocess errors and returns empty string).
    """
    global _cached_token, _token_expiry
    now = time.time()
    if _cached_token and now < _token_expiry:
        return _cached_token

    # 1. Attempt token acquisition via GCP Agent Runtime / Compute metadata server
    try:
        url = (
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?audience={target_audience}"
        )
        req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            token = resp.read().decode("utf-8").strip()
            if token:
                _cached_token = token
                _token_expiry = now + 3000.0  # Cache for 50 minutes
                return token
    except Exception:
        pass

    # 2. Local development fallback using active gcloud CLI session
    try:
        res = subprocess.run(
            ["gcloud", "auth", "print-identity-token"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            token = res.stdout.strip()
            _cached_token = token
            _token_expiry = now + 3000.0
            return token
    except Exception as exc:
        logger.debug("Local gcloud token retrieval skipped: %s", exc)

    return ""


def resolve_effective_auth_mode() -> str:
    """
    Summary:
        Determines the active Apigee authentication strategy based on APIGEE_AUTH_MODE
        and configured credential environment variables.

    Parameters:
        None.

    Return Value:
        str: Effective authentication mode ('none', 'api_key', 'bearer',
             'google_id_token', 'basic', 'custom_header', or 'multi').

    Exceptions/Errors:
        None.
    """
    mode = agent_settings.APIGEE_AUTH_MODE.strip().lower()
    if mode != "auto":
        return "bearer" if mode == "static_token" else ("google_id_token" if mode == "oidc" else mode)

    # Auto-detection order:
    # 1. Explicit cloud_run target or .run.app URL -> google_id_token
    mcp_url = agent_settings.MCP_SERVER_URL.strip().rstrip("/")
    target = agent_settings.MCP_DEPLOYMENT_TARGET.strip().lower()
    if target in ("cloud_run", "cloud", "gcp") or (
        mcp_url.startswith("https://") and "run.app" in mcp_url
    ):
        return "google_id_token"

    # 2. Check which Apigee credentials are populated in the environment
    has_api_key = bool(agent_settings.APIGEE_API_KEY.strip())
    has_bearer = bool(
        agent_settings.APIGEE_BEARER_TOKEN.strip() or agent_settings.MCP_AUTH_TOKEN.strip()
    )
    has_custom = bool(
        agent_settings.APIGEE_CUSTOM_HEADER_NAME.strip()
        and agent_settings.APIGEE_CUSTOM_HEADER_VALUE.strip()
    )
    has_basic = bool(
        agent_settings.APIGEE_BASIC_USER.strip() and agent_settings.APIGEE_BASIC_PASSWORD.strip()
    )

    active_Count = sum([has_api_key, has_bearer, has_custom, has_basic])
    if active_Count > 1:
        return "multi"
    if has_api_key:
        return "api_key"
    if has_bearer:
        return "bearer"
    if has_custom:
        return "custom_header"
    if has_basic:
        return "basic"
    return "none"


def build_mcp_headers(
    user_email: Optional[str] = None,
    trace_id: Optional[str] = None,
    caller_agent: Optional[str] = None,
) -> Dict[str, str]:
    """
    Summary:
        Builds the complete set of authentication, identity, and compliance audit headers
        for outbound requests from GCP Agent Runtime to the On-Premises Apigee API Proxy
        and MCP Server.

    Parameters:
        user_email (Optional[str]): Authenticated human user email from Gemini Enterprise App.
        trace_id (Optional[str]): Distributed execution trace ID from ADK ToolContext.
        caller_agent (Optional[str]): Agent name or identity string.

    Return Value:
        Dict[str, str]: Dictionary of HTTP headers to attach to the outbound MCP call.

    Exceptions/Errors:
        None.
    """
    mcp_url = agent_settings.MCP_SERVER_URL.strip().rstrip("/")
    effective_user = user_email or agent_settings.DEFAULT_END_USER_EMAIL

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": agent_settings.USER_AGENT,
        "X-Agent-Identity": agent_settings.AGENT_IDENTITY,
        "X-Agent-Name": caller_agent or agent_settings.AGENT_NAME,
        "X-Goog-Authenticated-User-Email": effective_user,
        "X-End-User-Email": effective_user,
        "X-User-Email": effective_user,
    }

    # Propagate distributed trace context for GCP Cloud Trace and on-prem Loki correlation
    if trace_id:
        headers["X-Cloud-Trace-Context"] = f"{trace_id}/0;o=1"
        headers["X-Trace-ID"] = trace_id

    auth_mode = resolve_effective_auth_mode()

    if auth_mode == "none":
        # No authentication headers attached (trust enforced by Cloud VPN / mTLS / IP allowlist)
        return headers

    if auth_mode == "google_id_token":
        audience = (agent_settings.APIGEE_OIDC_AUDIENCE or mcp_url).strip()
        id_token = _get_cloud_run_id_token(audience)
        if id_token:
            headers["Authorization"] = f"Bearer {id_token}"
            headers["X-Serverless-Authorization"] = f"Bearer {id_token}"
        return headers

    if auth_mode == "api_key":
        key_val = (
            agent_settings.APIGEE_API_KEY.strip() or agent_settings.MCP_AUTH_TOKEN.strip()
        )
        header_name = agent_settings.APIGEE_API_KEY_HEADER.strip() or "x-apikey"
        if key_val:
            headers[header_name] = key_val
        return headers

    if auth_mode == "bearer":
        token_val = (
            agent_settings.APIGEE_BEARER_TOKEN.strip() or agent_settings.MCP_AUTH_TOKEN.strip()
        )
        if token_val:
            headers["Authorization"] = f"Bearer {token_val}"
            headers["X-MCP-Token"] = token_val
        return headers

    if auth_mode == "basic":
        user = agent_settings.APIGEE_BASIC_USER.strip()
        password = agent_settings.APIGEE_BASIC_PASSWORD.strip()
        if user or password:
            encoded = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    if auth_mode == "custom_header":
        h_name = agent_settings.APIGEE_CUSTOM_HEADER_NAME.strip()
        h_val = agent_settings.APIGEE_CUSTOM_HEADER_VALUE.strip()
        if h_name and h_val:
            headers[h_name] = h_val
        return headers

    if auth_mode == "multi":
        # In 'auto' mode when multiple credentials are provided, attach all configured headers
        # so Apigee policies (VerifyAPIKey and/or Bearer verification) succeed seamlessly.
        if agent_settings.APIGEE_API_KEY.strip():
            header_name = agent_settings.APIGEE_API_KEY_HEADER.strip() or "x-apikey"
            headers[header_name] = agent_settings.APIGEE_API_KEY.strip()
        token_val = (
            agent_settings.APIGEE_BEARER_TOKEN.strip() or agent_settings.MCP_AUTH_TOKEN.strip()
        )
        if token_val:
            headers["Authorization"] = f"Bearer {token_val}"
            headers["X-MCP-Token"] = token_val
        if (
            agent_settings.APIGEE_CUSTOM_HEADER_NAME.strip()
            and agent_settings.APIGEE_CUSTOM_HEADER_VALUE.strip()
        ):
            headers[agent_settings.APIGEE_CUSTOM_HEADER_NAME.strip()] = (
                agent_settings.APIGEE_CUSTOM_HEADER_VALUE.strip()
            )
        return headers

    return headers


async def _dispatch_mcp_call(
    tool_name: str,
    arguments: Dict[str, Any],
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    Summary:
        Executes a remote tool call against the On-Premises MCP Server via the Apigee API Proxy
        over Cloud VPN using JSON-RPC 2.0 (`tools/call`). Emits structured telemetry logs for
        GCP Agent Runtime monitoring and Cloud Trace correlation.

    Parameters:
        tool_name (str): Name of the MCP tool to execute.
        arguments (Dict[str, Any]): Dictionary of tool input parameters.
        tool_context (Optional[ToolContext]): ADK execution context containing user_id and invocation_id.

    Return Value:
        Dict[str, Any]: Parsed result dictionary returned by the MCP tool or structured error payload.

    Exceptions/Errors:
        None (all transport, HTTP, and JSON-RPC errors are caught and returned as structured dicts).
    """
    user_email = None
    trace_id = None
    agent_name = None

    if tool_context:
        user_email = getattr(tool_context, "user_id", None)
        trace_id = getattr(tool_context, "invocation_id", None)
        agent_name = getattr(tool_context, "agent_name", None)

    effective_user = user_email or agent_settings.DEFAULT_END_USER_EMAIL
    auth_mode = resolve_effective_auth_mode()
    headers = build_mcp_headers(
        user_email=effective_user,
        trace_id=trace_id,
        caller_agent=agent_name,
    )

    mcp_endpoint = agent_settings.mcp_endpoint
    rpc_id = int(time.time() * 1000) % 1000000
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
            "_meta": {
                "user_email": effective_user,
                "caller_identity": agent_settings.AGENT_IDENTITY,
            },
        },
    }

    start_time = time.perf_counter()
    _emit_agent_runtime_log(
        logging.INFO,
        "mcp_tool_call_started",
        trace_id=trace_id,
        tool_name=tool_name,
        mcp_endpoint=mcp_endpoint,
        auth_mode=auth_mode,
        user_email=effective_user,
        rpc_id=rpc_id,
    )

    try:
        async with httpx.AsyncClient(
            timeout=agent_settings.MCP_TIMEOUT_SECONDS,
            verify=agent_settings.APIGEE_VERIFY_SSL,
        ) as client:
            resp = await client.post(mcp_endpoint, json=payload, headers=headers)
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

            if resp.status_code != 200:
                _emit_agent_runtime_log(
                    logging.ERROR,
                    "mcp_tool_call_http_error",
                    trace_id=trace_id,
                    tool_name=tool_name,
                    mcp_endpoint=mcp_endpoint,
                    auth_mode=auth_mode,
                    http_status=resp.status_code,
                    duration_ms=duration_ms,
                )
                return {
                    "status": "error",
                    "code": resp.status_code,
                    "message": (
                        f"Apigee / MCP server responded with HTTP status {resp.status_code}: "
                        f"{resp.text[:200]}"
                    ),
                }

            response_data = resp.json()
            if "error" in response_data and response_data["error"]:
                err = response_data["error"]
                err_code = err.get("code") if isinstance(err, dict) else None
                err_msg = (
                    err.get("message", "Unknown MCP execution error")
                    if isinstance(err, dict)
                    else str(err)
                )

                # Detect optional Apigee Model Armor security blocks (-32001 / -32002)
                if err_code == MODEL_ARMOR_BLOCK_CODE or str(err_msg).startswith(
                    "Blocked by Model Armor"
                ):
                    _emit_agent_runtime_log(
                        logging.WARNING,
                        "mcp_tool_blocked_by_model_armor",
                        trace_id=trace_id,
                        tool_name=tool_name,
                        duration_ms=duration_ms,
                        rpc_code=err_code,
                    )
                    return {
                        "status": "blocked_by_security_policy",
                        "tool": tool_name,
                        "message": (
                            "The response for this tool call was blocked by the Apigee Model Armor "
                            "security policy."
                        ),
                    }

                if err_code == SECURITY_GATEWAY_UNAVAILABLE_CODE:
                    _emit_agent_runtime_log(
                        logging.ERROR,
                        "mcp_tool_security_gateway_unavailable",
                        trace_id=trace_id,
                        tool_name=tool_name,
                        duration_ms=duration_ms,
                        rpc_code=err_code,
                    )
                    return {
                        "status": "security_gateway_unavailable",
                        "tool": tool_name,
                        "message": "The Apigee security screening service is temporarily unavailable.",
                    }

                _emit_agent_runtime_log(
                    logging.ERROR,
                    "mcp_tool_call_rpc_error",
                    trace_id=trace_id,
                    tool_name=tool_name,
                    duration_ms=duration_ms,
                    rpc_code=err_code,
                    error_message=err_msg[:200],
                )
                return {"status": "error", "error": err_msg}

            _emit_agent_runtime_log(
                logging.INFO,
                "mcp_tool_call_completed",
                trace_id=trace_id,
                tool_name=tool_name,
                mcp_endpoint=mcp_endpoint,
                auth_mode=auth_mode,
                http_status=resp.status_code,
                duration_ms=duration_ms,
            )

            # Unwrap standard MCP CallToolResult content list
            result_obj = response_data.get("result", {})
            content_list = result_obj.get("content", [])
            if content_list and isinstance(content_list, list):
                first_item = content_list[0]
                if isinstance(first_item, dict) and first_item.get("type") == "text":
                    text_val = first_item.get("text", "{}")
                    try:
                        return json.loads(text_val)
                    except json.JSONDecodeError:
                        return {"result": text_val}

            return result_obj
    except httpx.ConnectError as conn_err:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        _emit_agent_runtime_log(
            logging.ERROR,
            "mcp_tool_call_connect_error",
            trace_id=trace_id,
            tool_name=tool_name,
            mcp_endpoint=mcp_endpoint,
            duration_ms=duration_ms,
            error=str(conn_err),
        )
        return {
            "status": "error",
            "message": (
                f"Apigee MCP proxy at {mcp_endpoint} is currently unreachable over Cloud VPN. "
                "Please verify Cloud VPN tunnel status, PSC network attachment, or Apigee ingress."
            ),
        }
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        _emit_agent_runtime_log(
            logging.ERROR,
            "mcp_tool_call_unexpected_error",
            trace_id=trace_id,
            tool_name=tool_name,
            mcp_endpoint=mcp_endpoint,
            duration_ms=duration_ms,
            error=str(exc),
        )
        return {"status": "error", "message": f"Execution error: {str(exc)}"}


# ==============================================================================
# The 8 Strict Telecom MCP Tools Exposed to Google ADK Agent
# ==============================================================================


async def search_customer(
    phone_number: str = "",
    query: str = "",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Finds subscriber records by Mobile or Landline phone number (or account query). The phone number is unmasked as the unique subscriber identity; subscriber names and personal PII are strictly masked.

    Args:
        phone_number: Subscriber Mobile or Landline phone number (e.g. '+1 (555) 234-5678' or digits '5552345678'). Primary unique identity.
        query: Alternative search keyword: phone number, account ID, or general search.

    Returns:
        Dictionary containing matched subscriber records and status.
    """
    args: Dict[str, Any] = {}
    if phone_number:
        args["phone_number"] = phone_number
    if query:
        args["query"] = query
    return await _dispatch_mcp_call("search_customer", args, tool_context)


async def get_customer_360(
    phone_number: str = "",
    customer_id: str = "",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Returns consolidated subscriber profile (active broadband/mobile subscriptions, equipment, recent bills, and tickets) driven by Mobile or Landline phone number. Subscriber name is strictly masked.

    Args:
        phone_number: Subscriber Mobile or Landline phone number used as primary identity.
        customer_id: Internal Customer UUID (optional if phone_number is supplied).

    Returns:
        Dictionary containing 360-degree customer profile including plans, equipment, and recent bills.
    """
    args: Dict[str, Any] = {}
    if phone_number:
        args["phone_number"] = phone_number
    if customer_id:
        args["customer_id"] = customer_id
    return await _dispatch_mcp_call("get_customer_360", args, tool_context)


async def get_service_diagnostics(
    phone_number: str = "",
    service_id: str = "",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Runs remote line diagnostics for Home Broadband ONT or Mobile 5G lines. Accepts subscriber phone number or subscription UUID. Returns dBm signal, packet loss, and recommended fix.

    Args:
        phone_number: Subscriber Mobile or Landline phone number to automatically diagnose their active service.
        service_id: Subscription UUID (optional if phone_number is supplied).

    Returns:
        Dictionary containing diagnostic metrics (optical dBm, latency, packet loss, outage status, remediation steps).
    """
    args: Dict[str, Any] = {}
    if phone_number:
        args["phone_number"] = phone_number
    if service_id:
        args["service_id"] = service_id
    return await _dispatch_mcp_call("get_service_diagnostics", args, tool_context)


async def run_remote_device_action(
    action: str,
    phone_number: str = "",
    device_id: str = "",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Executes remote maintenance commands on customer hardware: 'reboot', 'channel_optimization', or 'ping_sweep'. Accepts subscriber phone number or device UUID.

    Args:
        action: Action to execute ('reboot', 'channel_optimization', or 'ping_sweep').
        phone_number: Subscriber Mobile or Landline phone number to locate and reboot premise equipment.
        device_id: Hardware device UUID (optional if phone_number is supplied).

    Returns:
        Dictionary detailing the outcome and verification of the remote device action.
    """
    args: Dict[str, Any] = {"action": action}
    if phone_number:
        args["phone_number"] = phone_number
    if device_id:
        args["device_id"] = device_id
    return await _dispatch_mcp_call("run_remote_device_action", args, tool_context)


async def check_network_outages(
    phone_number: str = "",
    postal_code: str = "",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Checks for ongoing infrastructure fiber cuts or 5G cell tower maintenance in a given postal area or for a subscriber's phone number.

    Args:
        phone_number: Subscriber Mobile or Landline phone number to resolve their service area.
        postal_code: Customer 5-digit postal code (optional if phone_number is supplied).

    Returns:
        Dictionary listing active, investigating, or resolved network incidents in the area.
    """
    args: Dict[str, Any] = {}
    if phone_number:
        args["phone_number"] = phone_number
    if postal_code:
        args["postal_code"] = postal_code
    return await _dispatch_mcp_call("check_network_outages", args, tool_context)


async def get_billing_breakdown(
    phone_number: str = "",
    account_id: str = "",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Returns itemized invoice records, roaming fees, and payment status with masked card numbers. Accepts subscriber phone number or account ID.

    Args:
        phone_number: Subscriber Mobile or Landline phone number.
        account_id: Account UUID or Account Number (optional if phone_number is supplied).

    Returns:
        Dictionary containing billing invoices, line items, roaming surcharges, and payment history.
    """
    args: Dict[str, Any] = {}
    if phone_number:
        args["phone_number"] = phone_number
    if account_id:
        args["account_id"] = account_id
    return await _dispatch_mcp_call("get_billing_breakdown", args, tool_context)


async def get_upsell_recommendations(
    phone_number: str = "",
    customer_id: str = "",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Computes personalized upgrade offers (Gigabit fiber, Unlimited 5G pass, Roaming bundle) and provides agent pitch scripts. Identified by subscriber phone number or UUID.

    Args:
        phone_number: Subscriber Mobile or Landline phone number.
        customer_id: Customer UUID (optional if phone_number is supplied).

    Returns:
        Dictionary with targeted plan offers, calculated monthly savings, and agent pitch scripts.
    """
    args: Dict[str, Any] = {}
    if phone_number:
        args["phone_number"] = phone_number
    if customer_id:
        args["customer_id"] = customer_id
    return await _dispatch_mcp_call("get_upsell_recommendations", args, tool_context)


async def log_agent_interaction(
    issue_summary: str,
    resolution_summary: str,
    phone_number: str = "",
    customer_id: str = "",
    agent_name: str = "",
    call_duration_sec: int = 0,
    upsell_offered: bool = False,
    upsell_accepted: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Logs agent call notes, resolution status, and upsell outcome directly into the CRM database and audit trail. Identified by subscriber phone number or UUID.

    Args:
        issue_summary: Problem described by customer.
        resolution_summary: Solution provided by the agent.
        phone_number: Subscriber Mobile or Landline phone number.
        customer_id: Customer UUID (optional if phone_number is supplied).
        agent_name: Agent name or ID.
        call_duration_sec: Call duration in seconds.
        upsell_offered: Was an upsell offer pitched.
        upsell_accepted: Did the customer accept the upsell offer.

    Returns:
        Dictionary confirming interaction ID logged in the database and audit trail.
    """
    args: Dict[str, Any] = {
        "issue_summary": issue_summary,
        "resolution_summary": resolution_summary,
        "call_duration_sec": call_duration_sec,
        "upsell_offered": upsell_offered,
        "upsell_accepted": upsell_accepted,
    }
    if phone_number:
        args["phone_number"] = phone_number
    if customer_id:
        args["customer_id"] = customer_id
    if agent_name:
        args["agent_name"] = agent_name
    return await _dispatch_mcp_call("log_agent_interaction", args, tool_context)


# Ordered list of all 8 native ADK tools matching the MCP catalog
ALL_TELECOM_TOOLS = [
    search_customer,
    get_customer_360,
    get_service_diagnostics,
    run_remote_device_action,
    check_network_outages,
    get_billing_breakdown,
    get_upsell_recommendations,
    log_agent_interaction,
]


def create_dynamic_mcp_toolset() -> McpToolset:
    """
    Summary:
        Instantiates an ADK McpToolset connecting to the remote FastMCP server via Apigee
        over Streamable HTTP with per-call identity and authentication header injection.

    Parameters:
        None.

    Return Value:
        McpToolset: Configured ADK MCP toolset instance.

    Exceptions/Errors:
        None.
    """
    mcp_url = agent_settings.mcp_endpoint

    def dynamic_headers(ctx) -> Dict[str, str]:
        user_email = getattr(ctx, "user_id", None)
        trace_id = getattr(ctx, "invocation_id", None)
        agent_name = getattr(ctx, "agent_name", None)
        return build_mcp_headers(
            user_email=user_email,
            trace_id=trace_id,
            caller_agent=agent_name,
        )

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=mcp_url,
            timeout=agent_settings.MCP_TIMEOUT_SECONDS,
        ),
        header_provider=dynamic_headers,
    )
