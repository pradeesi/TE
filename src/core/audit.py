"""
Purpose: Enterprise audit logging engine with direct Grafana Loki streaming for the MCP server
         and CRM console.
Architecture/Context: Dispatches structured JSON audit trails for every MCP HTTP request and tool
                      execution, including FULL RAW request/response payloads. Loki is therefore
                      a restricted PII store (explicit design decision): masking happens later,
                      in Apigee + Model Armor, on the path to the AI agent - not in this log.
Dependencies/Side Effects:
    - Pushes to ${LOKI_URL}/loki/api/v1/push with httpx (async, short timeouts) as a background
      task so audit shipping never blocks or fails the MCP request path.
    - When settings.LOKI_AUTH_MODE == 'google_id_token' (Loki on IAM-protected Cloud Run), an
      'Authorization: Bearer <id_token>' header is attached. The ID token is fetched from the GCP
      metadata server (audience = LOKI_URL without trailing slash), cached ~50 minutes, and
      refreshed on expiry or on a 401/403 from Loki. GCE_METADATA_HOST (standard Google client
      library variable) may override the metadata host, e.g. for tests.
    - A compact summary line is always written to stdout; the full audit line is written to
      stdout only when Loki is not configured or the push fails (structured fallback).
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set

import httpx

from src.core.config import settings

# Configure structured system logger
logger = logging.getLogger("hybrid_mcp.audit")
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)

# Google ID tokens live for 60 minutes; refreshing after 50 leaves margin for clock skew.
ID_TOKEN_CACHE_SECONDS = 3000.0
# After a metadata-server failure, wait before retrying so a broken environment does not
# add a metadata round-trip to every single audit push.
ID_TOKEN_FAILURE_BACKOFF_SECONDS = 30.0
METADATA_TIMEOUT_SECONDS = 1.5
LOKI_PUSH_TIMEOUT_SECONDS = 2.0

# Header names whose values must never be written to the audit store verbatim.
SECRET_HEADER_NAMES = {"x-apikey", "cookie", "proxy-authorization"}
TOKEN_HEADER_NAMES = {"authorization", "x-serverless-authorization"}


def _redact_token_value(value: str) -> str:
    """
    Summary:
        Redacts the signature segment of a bearer JWT (keeping header/claims for audit
        forensics) or fully redacts opaque bearer tokens.

    Parameters:
        value (str): Raw Authorization header value.

    Return Value:
        str: Redacted header value safe to persist.

    Exceptions/Errors:
        None.
    """
    raw = (value or "").strip()
    prefix = ""
    if raw.lower().startswith("bearer "):
        prefix, raw = "Bearer ", raw[7:].strip()
    parts = raw.split(".")
    if len(parts) == 3 and raw.startswith("eyJ"):
        return f"{prefix}{parts[0]}.{parts[1]}.[REDACTED_SIGNATURE]"
    return f"{prefix}[REDACTED]" if raw else value


def sanitize_audit_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Summary:
        Returns a copy of request headers with credentials redacted before they are logged.
        PII in bodies is intentionally NOT redacted (Loki is the restricted raw PII store);
        credentials are, because replaying them would grant access.

    Parameters:
        headers (Dict[str, str]): Incoming HTTP headers.

    Return Value:
        Dict[str, str]: Sanitized header mapping.

    Exceptions/Errors:
        None.
    """
    sanitized: Dict[str, str] = {}
    for name, value in (headers or {}).items():
        lowered = name.lower()
        if lowered in SECRET_HEADER_NAMES:
            sanitized[name] = "[REDACTED]"
        elif lowered in TOKEN_HEADER_NAMES:
            sanitized[name] = _redact_token_value(str(value))
        else:
            sanitized[name] = value
    return sanitized


class LokiAuditClient:
    """
    Summary:
        Lightweight client for asynchronously pushing structured log streams into Grafana Loki,
        with optional Google ID-token authentication for IAM-protected Loki on Cloud Run.

    Parameters:
        See __init__.

    Return Value:
        N/A (class).

    Exceptions/Errors:
        Never raises from logging methods; failures are logged and fall back to stdout.
    """

    def __init__(self, loki_url: Optional[str] = None, auth_mode: Optional[str] = None):
        """
        Summary:
            Initializes the Loki client with endpoint URL, auth mode and token cache.

        Parameters:
            loki_url (Optional[str]): Base URL of Loki (e.g. http://loki:3100). Blank disables
                shipping. Defaults to settings.LOKI_URL.
            auth_mode (Optional[str]): 'none' or 'google_id_token'. Defaults to
                settings.LOKI_AUTH_MODE.

        Return Value:
            None.

        Exceptions/Errors:
            ValueError: If auth_mode is not a supported value.
        """
        base_url = (loki_url if loki_url is not None else settings.LOKI_URL) or ""
        self.loki_url = base_url.strip().rstrip("/")
        self.auth_mode = (auth_mode or settings.LOKI_AUTH_MODE).strip().lower()
        if self.auth_mode not in ("none", "google_id_token"):
            raise ValueError(f"Unsupported Loki auth mode '{self.auth_mode}'.")

        self._push_endpoint = f"{self.loki_url}/loki/api/v1/push" if self.loki_url else None
        self._audience = self.loki_url
        metadata_host = os.environ.get("GCE_METADATA_HOST", "metadata.google.internal").strip()
        self._metadata_identity_url = (
            f"http://{metadata_host}/computeMetadata/v1/instance/service-accounts/default/identity"
        )

        self._http_client: Optional[httpx.AsyncClient] = None
        self._client_loop: Optional[asyncio.AbstractEventLoop] = None
        self._cached_token = ""
        self._token_expiry = 0.0
        self._token_backoff_until = 0.0
        self._token_lock: Optional[asyncio.Lock] = None
        self._pending: Set[asyncio.Task] = set()

    def _client(self) -> httpx.AsyncClient:
        """
        Summary:
            Returns an httpx client bound to the currently running event loop, recreating it if
            the loop changed (test runners and some servers spin up fresh loops).

        Parameters:
            None.

        Return Value:
            httpx.AsyncClient: Client with short timeouts.

        Exceptions/Errors:
            RuntimeError: If called outside a running event loop.
        """
        loop = asyncio.get_running_loop()
        if self._http_client is None or self._client_loop is not loop or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=LOKI_PUSH_TIMEOUT_SECONDS)
            self._client_loop = loop
            self._token_lock = asyncio.Lock()
        return self._http_client

    def invalidate_token(self) -> None:
        """
        Summary:
            Drops the cached ID token so the next push fetches a fresh one.

        Parameters:
            None.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        self._cached_token = ""
        self._token_expiry = 0.0

    async def _fetch_id_token(self) -> Optional[str]:
        """
        Summary:
            Fetches a Google-signed ID token for the Loki audience from the metadata server.

        Parameters:
            None.

        Return Value:
            Optional[str]: The token, or None if the metadata server is unavailable/errored.

        Exceptions/Errors:
            None. Failures are logged at WARNING level.
        """
        try:
            response = await self._client().get(
                self._metadata_identity_url,
                params={"audience": self._audience},
                headers={"Metadata-Flavor": "Google"},
                timeout=METADATA_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            logger.warning("Metadata server unreachable while fetching Loki ID token: %s", exc)
            return None

        token = response.text.strip()
        if response.status_code != 200 or not token:
            logger.warning(
                "Metadata server returned HTTP %d while fetching Loki ID token.", response.status_code
            )
            return None
        return token

    async def get_auth_headers(self, force_refresh: bool = False) -> Dict[str, str]:
        """
        Summary:
            Returns the Authorization header for Loki pushes according to LOKI_AUTH_MODE.

        Parameters:
            force_refresh (bool): Ignore the cache (used after a 401/403 from Loki).

        Return Value:
            Dict[str, str]: {'Authorization': 'Bearer <token>'} in google_id_token mode when a
            token is available; an empty dict otherwise.

        Exceptions/Errors:
            None.
        """
        if self.auth_mode != "google_id_token" or not self._push_endpoint:
            return {}

        now = time.time()
        if not force_refresh and self._cached_token and now < self._token_expiry:
            return {"Authorization": f"Bearer {self._cached_token}"}
        if not force_refresh and now < self._token_backoff_until:
            return {}

        # _client() (re)binds the lock to the running loop; the fallback is purely defensive.
        self._client()
        lock = self._token_lock
        if lock is None:
            lock = self._token_lock = asyncio.Lock()
        async with lock:
            # Another coroutine may have refreshed the token while we waited for the lock.
            now = time.time()
            if not force_refresh and self._cached_token and now < self._token_expiry:
                return {"Authorization": f"Bearer {self._cached_token}"}

            token = await self._fetch_id_token()
            if not token:
                self._token_backoff_until = now + ID_TOKEN_FAILURE_BACKOFF_SECONDS
                return {}

            self._cached_token = token
            self._token_expiry = now + ID_TOKEN_CACHE_SECONDS
            self._token_backoff_until = 0.0
            return {"Authorization": f"Bearer {token}"}

    async def push_to_loki(self, labels: Dict[str, str], log_line: str) -> bool:
        """
        Summary:
            Pushes one log line to Loki, retrying once with a fresh ID token on 401/403.

        Parameters:
            labels (Dict[str, str]): Loki stream labels.
            log_line (str): Serialized JSON audit record.

        Return Value:
            bool: True if Loki acknowledged the push (HTTP 200/204), False otherwise
            (including when shipping is disabled).

        Exceptions/Errors:
            None. Transport errors are logged at WARNING level.
        """
        if not self._push_endpoint:
            return False

        loki_payload = {"streams": [{"stream": labels, "values": [[str(time.time_ns()), log_line]]}]}

        for attempt in range(2):
            headers = {"Content-Type": "application/json"}
            headers.update(await self.get_auth_headers(force_refresh=attempt > 0))
            try:
                response = await self._client().post(self._push_endpoint, json=loki_payload, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("Loki push failed (%s: %s).", type(exc).__name__, exc)
                return False

            if response.status_code in (200, 204):
                return True
            if response.status_code in (401, 403) and self.auth_mode == "google_id_token" and attempt == 0:
                # Token rejected (expired early or audience rotated): refresh once and retry.
                self.invalidate_token()
                continue
            logger.warning("Loki returned non-success response %d: %s", response.status_code, response.text[:300])
            return False
        return False

    async def _ship(self, labels: Dict[str, str], log_line: str, summary: str) -> None:
        """
        Summary:
            Background worker: pushes to Loki and falls back to a full structured stdout record
            when shipping is disabled or fails.

        Parameters:
            labels (Dict[str, str]): Loki stream labels.
            log_line (str): Full JSON audit record.
            summary (str): Short description used in the fallback log message.

        Return Value:
            None.

        Exceptions/Errors:
            None; unexpected errors are logged with stack trace so background tasks never die silently.
        """
        try:
            if await self.push_to_loki(labels, log_line):
                return
        except Exception as exc:  # noqa: BLE001 - background task must never raise unobserved
            logger.error("Unexpected error while shipping audit record to Loki: %s", exc, exc_info=True)

        if self._push_endpoint:
            logger.warning("AUDIT_FALLBACK (%s) Loki unavailable, writing full record to stdout.", summary)
        logger.info("AUDIT_RECORD: %s", log_line)

    def _dispatch(self, labels: Dict[str, str], log_line: str, summary: str) -> None:
        """
        Summary:
            Schedules shipping as a background task so the MCP request path never waits on Loki.

        Parameters:
            labels (Dict[str, str]): Loki stream labels.
            log_line (str): Full JSON audit record.
            summary (str): Short description for logs.

        Return Value:
            None.

        Exceptions/Errors:
            None. Without a running loop the record is written to stdout synchronously.
        """
        logger.info("AUDIT_SUMMARY: %s", summary)
        if not self._push_endpoint:
            logger.info("AUDIT_RECORD: %s", log_line)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info("AUDIT_RECORD: %s", log_line)
            return
        task = loop.create_task(self._ship(labels, log_line, summary))
        # Keep a strong reference so the task is not garbage-collected mid-flight.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _base_labels(self, event_type: str, tool: str, caller: str, caller_type: str,
                     end_user: str, status: str, auth_method: str) -> Dict[str, str]:
        """
        Summary:
            Builds the Loki stream label set shared by all audit records.

        Parameters:
            event_type (str): Audit event type.
            tool (str): Tool name or endpoint path.
            caller (str): Calling AI agent.
            caller_type (str): Caller category (AI_AGENT).
            end_user (str): Human operator email.
            status (str): Outcome status.
            auth_method (str): How Cloud Run authenticated the request.

        Return Value:
            Dict[str, str]: Label mapping.

        Exceptions/Errors:
            None.
        """
        return {
            "job": "telecom-mcp-audit",
            "app": settings.SERVICE_NAME,
            "env": settings.APP_ENV,
            "event_type": event_type,
            "tool": tool,
            "caller": caller,
            "caller_type": caller_type,
            "end_user": end_user,
            "status": status,
            "auth_method": auth_method,
        }

    @staticmethod
    def _context_fields(security_context: Optional[Dict[str, Any]], caller_identity: str) -> Dict[str, Any]:
        """
        Summary:
            Normalizes security-context fields with safe defaults.

        Parameters:
            security_context (Optional[Dict[str, Any]]): Context from extract_security_context.
            caller_identity (str): Fallback caller identity.

        Return Value:
            Dict[str, Any]: Normalized audit identity fields.

        Exceptions/Errors:
            None.
        """
        sec_ctx = security_context or {}
        return {
            "caller": sec_ctx.get("caller", caller_identity or "telecom-adk-agent"),
            "caller_type": sec_ctx.get("caller_type", "AI_AGENT"),
            "end_user_email": sec_ctx.get("end_user_email", settings.DEFAULT_END_USER_EMAIL),
            "end_user_id": sec_ctx.get("end_user_id", "N/A"),
            "client_ip": sec_ctx.get("client_ip", "N/A"),
            "user_agent": sec_ctx.get("user_agent", "N/A"),
            "trace_id": sec_ctx.get("trace_id", "N/A"),
            "auth_method": sec_ctx.get("auth_method", "IAM_EDGE_PERIMETER"),
            "apigee_developer_app": sec_ctx.get("apigee_developer_app", "N/A"),
            "apigee_client_id": sec_ctx.get("apigee_client_id", "N/A"),
            "apigee_proxy": sec_ctx.get("apigee_proxy", "N/A"),
        }

    async def log_event(
        self,
        event_type: str,
        caller_identity: str,
        tool_name: str,
        details: Optional[Dict[str, Any]] = None,
        status: str = "SUCCESS",
        customer_id: Optional[str] = None,
        security_context: Optional[Dict[str, Any]] = None,
        request_payload: Optional[Dict[str, Any]] = None,
        response_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Summary:
            Emits a structured audit record (full raw request and response payloads) to Loki,
            with stdout fallback. Returns immediately; shipping runs in the background.

        Parameters:
            event_type (str): Type of audit event (e.g. 'MCP_TOOL_EXECUTION').
            caller_identity (str): Identity of the calling agent, user, or service.
            tool_name (str): Name of the invoked tool or operation.
            details (Optional[Dict[str, Any]]): Additional structured event details.
            status (str): Execution status ('SUCCESS', 'FAILED', 'NOT_FOUND').
            customer_id (Optional[str]): Target customer ID if applicable.
            security_context (Optional[Dict[str, Any]]): Security attributes (IP, trace ID, Apigee app).
            request_payload (Optional[Dict[str, Any]]): Exact tool arguments sent by caller.
            response_payload (Optional[Dict[str, Any]]): Exact RAW response payload returned to caller.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        ctx = self._context_fields(security_context, caller_identity)
        payload_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": event_type,
            **ctx,
            "tool": tool_name,
            "status": status,
            "customer_id": customer_id or "N/A",
            "request": request_payload if request_payload is not None else (details or {}),
            "response": response_payload if response_payload is not None else {},
            "details": details or {},
        }
        log_line = json.dumps(payload_data, default=str)
        labels = self._base_labels(event_type, tool_name, ctx["caller"], ctx["caller_type"],
                                   ctx["end_user_email"], status, ctx["auth_method"])
        summary = f"event={event_type} tool={tool_name} status={status} trace={ctx['trace_id']}"
        self._dispatch(labels, log_line, summary)

    async def log_http_request(
        self,
        endpoint: str,
        method: str,
        headers: Dict[str, str],
        body: Any,
        caller_identity: str,
        security_context: Optional[Dict[str, Any]] = None,
        jwt_claims: Optional[Dict[str, Any]] = None,
        status: str = "RECEIVED"
    ) -> None:
        """
        Summary:
            Logs the complete incoming HTTP request (credential-redacted headers, raw body and
            decoded JWT claims) to Loki with stdout fallback.

        Parameters:
            endpoint (str): Request URL path (e.g. '/mcp', '/messages', '/execute').
            method (str): HTTP method ('POST', 'GET').
            headers (Dict[str, str]): Complete incoming HTTP request headers mapping.
            body (Any): Complete message body (parsed JSON or raw text).
            caller_identity (str): Resolved identity of the calling agent.
            security_context (Optional[Dict[str, Any]]): Security and network context.
            jwt_claims (Optional[Dict[str, Any]]): Decoded (unverified) JWT claims, if any.
            status (str): Processing status ('RECEIVED', 'INVALID_JSON', 'LISTED', 'CONNECTED').

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        ctx = self._context_fields(security_context, caller_identity)
        payload_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": "MCP_HTTP_REQUEST",
            "endpoint": endpoint,
            "method": method,
            **ctx,
            "status": status,
            "headers": sanitize_audit_headers(headers),
            "jwt_claims": jwt_claims or {},
            "body": body,
        }
        log_line = json.dumps(payload_data, default=str)
        labels = self._base_labels("MCP_HTTP_REQUEST", endpoint, ctx["caller"], ctx["caller_type"],
                                   ctx["end_user_email"], status, ctx["auth_method"])
        summary = f"event=MCP_HTTP_REQUEST endpoint={endpoint} method={method} status={status} trace={ctx['trace_id']}"
        self._dispatch(labels, log_line, summary)

    async def flush(self, timeout: float = 5.0) -> None:
        """
        Summary:
            Waits for in-flight background shipments (used on shutdown and in tests).

        Parameters:
            timeout (float): Maximum seconds to wait.

        Return Value:
            None.

        Exceptions/Errors:
            None. Timeouts are logged at WARNING level.
        """
        pending = [t for t in self._pending if not t.done()]
        if not pending:
            return
        done, not_done = await asyncio.wait(pending, timeout=timeout)
        if not_done:
            logger.warning("%d audit shipments still pending after %.1fs flush.", len(not_done), timeout)

    async def close(self) -> None:
        """
        Summary:
            Flushes pending shipments and closes internal HTTP resources cleanly.

        Parameters:
            None.

        Return Value:
            None.

        Exceptions/Errors:
            None.
        """
        await self.flush()
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()


# Singleton audit client instance
audit_client = LokiAuditClient()
