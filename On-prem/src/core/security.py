"""
Purpose: Caller identity, audit security-context extraction, and phone normalization helpers
         for the HybridMCP-Apigee MCP server.
Architecture/Context: Authentication is NOT performed in application code. The MCP server runs
                      on Cloud Run with IAM enforced (no allUsers); only the Apigee proxy service
                      account and the Prometheus service account hold roles/run.invoker. By the
                      time a request reaches this code it has already been authenticated by
                      Cloud Run. This module therefore only *describes* the caller for the audit
                      trail (AI agent, end user, Apigee developer app, trace ID, client IP).
                      PII masking is also NOT done here: Apigee + Model Armor + Sensitive Data
                      Protection de-identify MCP tool responses before they reach the AI agent.
Dependencies/Side Effects: Reads settings.DEFAULT_END_USER_EMAIL. JWTs are decoded WITHOUT
                           signature verification strictly for audit labelling - never use the
                           decoded claims for authorization decisions.
"""

import base64
import binascii
import json
import logging
import re
from typing import Any, Dict, Optional

from src.core.config import settings

logger = logging.getLogger("hybrid_mcp.security")

# Fallback label for the direct MCP client when no agent identity header is present.
# The direct caller of the MCP server is always the ADK agent (via Agent Gateway -> Apigee).
DEFAULT_AGENT_CALLER = "telecom-adk-agent"

# End-user identity headers, in priority order. The ADK agent forwards the human operator's
# identity in x-end-user-email / x-user-email; x-goog-authenticated-user-email is set by IAP.
END_USER_EMAIL_HEADERS = (
    "x-end-user-email",
    "x-user-email",
    "x-goog-authenticated-user-email",
    "x-goog-user-email",
    "x-agent-email",
    "x-forwarded-email",
)

# Headers injected by the Apigee proxy (AssignMessage policy) describing the API consumer.
APIGEE_HEADERS = {
    "apigee_developer_app": "x-apigee-developer-app",
    "apigee_client_id": "x-apigee-client-id",
    "apigee_proxy": "x-apigee-proxy",
}


def _strip_bearer(token: Optional[str]) -> str:
    """
    Summary:
        Removes an optional 'Bearer ' prefix and surrounding whitespace from a token string.

    Parameters:
        token (Optional[str]): Raw Authorization header value or bare token.

    Return Value:
        str: Bare token, or an empty string when token is None/blank.

    Exceptions/Errors:
        None.
    """
    if not token or not isinstance(token, str):
        return ""
    cleaned = token.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned


def decode_jwt_unverified(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Summary:
        Decodes the payload of a JSON Web Token (JWT) without cryptographic verification.
        Used for audit logging and extracting identity claims from tokens that Cloud Run IAM
        has already validated at the edge.

    Parameters:
        token (Optional[str]): Bearer token string or raw JWT.

    Return Value:
        Optional[Dict[str, Any]]: Parsed payload dictionary if the token is base64url JSON,
        None for missing, opaque, or malformed tokens.

    Exceptions/Errors:
        None. Decoding failures are logged at DEBUG level and reported as None.
    """
    cleaned = _strip_bearer(token)
    if not cleaned:
        return None

    parts = cleaned.split(".")
    if len(parts) not in (2, 3) or not cleaned.startswith("eyJ"):
        return None

    try:
        # base64url payloads are unpadded; restore padding before decoding.
        padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        logger.debug("Unable to decode JWT payload for audit context: %s", exc)
        return None

    return payload if isinstance(payload, dict) else None


def extract_caller_identity(
    token: Optional[str] = None,
    user_email_header: Optional[str] = None,
    custom_agent_header: Optional[str] = None,
    payload_user: Optional[str] = None,
    request_headers: Optional[Dict[str, str]] = None
) -> str:
    """
    Summary:
        Extracts the most specific human or agent identity available from end-user identity
        headers, agent identity headers, OIDC JWT claims, or tool payload metadata.
        Provides non-repudiation for the audit trail without requiring manual user input.

    Parameters:
        token (Optional[str]): Bearer token string or X-Serverless-Authorization string.
        user_email_header (Optional[str]): Explicit end-user email (e.g. IAP header value).
        custom_agent_header (Optional[str]): Value of X-Agent-Identity or X-Agent-Email.
        payload_user (Optional[str]): Explicit user email provided in MCP payload arguments or _meta.
        request_headers (Optional[Dict[str, str]]): Complete incoming HTTP request headers mapping.

    Return Value:
        str: Extracted identity (e.g. 'sarah.jenkins@telecom.example') or DEFAULT_AGENT_CALLER
        when nothing identifying is present.

    Exceptions/Errors:
        None.
    """
    headers = {k.lower(): v for k, v in (request_headers or {}).items()}

    # 1. End-user identity headers ('accounts.google.com:' prefix comes from IAP).
    user_candidates = [user_email_header] + [headers.get(h) for h in END_USER_EMAIL_HEADERS] + [
        headers.get("x-goog-authenticated-user-id"),
        headers.get("x-forwarded-user"),
    ]
    for candidate in user_candidates:
        if candidate and str(candidate).strip():
            clean = str(candidate).replace("accounts.google.com:", "").strip()
            if clean:
                return clean

    # 2. Custom agent identity headers.
    agent_candidates = [
        custom_agent_header,
        headers.get("x-agent-identity"),
        headers.get("x-caller-identity"),
    ]
    for candidate in agent_candidates:
        if candidate and str(candidate).strip():
            return str(candidate).strip()

    # 3. Decoded OIDC JWT claims (already validated by Cloud Run IAM).
    raw_token = token or headers.get("authorization") or headers.get("x-serverless-authorization")
    jwt_claims = decode_jwt_unverified(raw_token)
    if jwt_claims:
        email = str(jwt_claims.get("email") or "").strip()
        if email:
            if email.endswith("gserviceaccount.com"):
                return f"service-account:{email}"
            return email
        preferred = str(jwt_claims.get("preferred_username") or "").strip()
        if preferred:
            return preferred
        if jwt_claims.get("sub"):
            return f"user:{jwt_claims['sub']}"

    # 4. Explicit user email in MCP payload arguments or _meta payload.
    if payload_user and str(payload_user).strip():
        return str(payload_user).strip()

    return DEFAULT_AGENT_CALLER


def extract_security_context(
    token: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    payload_user: Optional[str] = None,
    client_ip: Optional[str] = None
) -> Dict[str, Any]:
    """
    Summary:
        Builds the security and audit context dictionary attached to every audit record.
        The immediate caller is always categorized as an AI_AGENT (the ADK agent reaching the
        server through Agent Gateway and Apigee). The initiating human operator is resolved
        from identity headers, non-service-account JWT claims, or MCP payload metadata, and
        the Apigee consumer metadata (developer app, client ID, proxy) is captured when present.

    Parameters:
        token (Optional[str]): Authorization token string (optional; headers are also inspected).
        headers (Optional[Dict[str, str]]): HTTP request headers mapping.
        payload_user (Optional[str]): Explicit user email from tool arguments or metadata.
        client_ip (Optional[str]): Remote socket IP address.

    Return Value:
        Dict[str, Any]: Keys caller, caller_identity, caller_type, end_user_email, end_user_id,
        client_ip, user_agent, trace_id, auth_method, apigee_developer_app, apigee_client_id,
        apigee_proxy. Missing Apigee values are reported as 'N/A'.

    Exceptions/Errors:
        None.
    """
    hdr = {k.lower(): v for k, v in (headers or {}).items()}
    raw_token = token or hdr.get("authorization") or hdr.get("x-serverless-authorization")
    jwt_claims = decode_jwt_unverified(raw_token) or {}

    apigee_ctx = {
        key: (str(hdr[name]).strip() if hdr.get(name) and str(hdr[name]).strip() else "N/A")
        for key, name in APIGEE_HEADERS.items()
    }

    # 1. Resolve the calling AI agent. Explicit agent headers win; otherwise the Apigee
    #    developer app is the best available identifier of the agent that owns the API key.
    caller = DEFAULT_AGENT_CALLER
    if hdr.get("x-agent-identity"):
        caller = str(hdr["x-agent-identity"]).strip()
    elif hdr.get("x-agent-name"):
        caller = str(hdr["x-agent-name"]).strip()
    elif apigee_ctx["apigee_developer_app"] != "N/A":
        caller = f"apigee-app:{apigee_ctx['apigee_developer_app']}"
    elif "curl" in hdr.get("user-agent", ""):
        caller = "curl-test-agent"

    # 2. Resolve the initiating human operator.
    end_user_email = None
    for header_name in END_USER_EMAIL_HEADERS:
        candidate = hdr.get(header_name)
        if candidate and str(candidate).strip():
            clean = str(candidate).replace("accounts.google.com:", "").strip()
            if clean and "@" in clean:
                end_user_email = clean
                break

    # Service-account emails identify Apigee/Prometheus, not a human, so they are skipped.
    if not end_user_email and jwt_claims.get("email"):
        email_claim = str(jwt_claims["email"]).strip()
        if not email_claim.endswith("gserviceaccount.com"):
            end_user_email = email_claim

    if not end_user_email and payload_user and "@" in str(payload_user):
        end_user_email = str(payload_user).strip()

    if not end_user_email:
        end_user_email = settings.DEFAULT_END_USER_EMAIL

    if hdr.get("x-goog-authenticated-user-id"):
        end_user_id = str(hdr["x-goog-authenticated-user-id"]).replace("accounts.google.com:", "").strip()
    elif jwt_claims.get("sub"):
        end_user_id = str(jwt_claims["sub"]).strip()
    else:
        end_user_id = "N/A"

    # 3. Resolve the originating client IP from the proxy chain (Apigee / Google front end).
    forwarded = hdr.get("x-forwarded-for")
    if forwarded:
        effective_ip = forwarded.split(",")[0].strip()
    elif hdr.get("x-real-ip"):
        effective_ip = str(hdr["x-real-ip"]).strip()
    else:
        effective_ip = client_ip or "N/A"

    # 4. Distributed tracing context for Cloud Trace / Apigee correlation.
    trace_header = hdr.get("x-cloud-trace-context") or hdr.get("x-trace-id") or ""
    trace_id = trace_header.split("/")[0] if "/" in trace_header else (trace_header or "N/A")

    # 5. Describe (not enforce) how Cloud Run authenticated the request.
    if jwt_claims:
        auth_method = "OIDC_ID_TOKEN"
    elif hdr.get("x-goog-authenticated-user-email"):
        auth_method = "IAP_ASSERTION"
    else:
        auth_method = "IAM_EDGE_PERIMETER"

    return {
        "caller": caller,
        "caller_identity": caller,
        "caller_type": "AI_AGENT",
        "end_user_email": end_user_email,
        "end_user_id": end_user_id,
        "client_ip": effective_ip,
        "user_agent": hdr.get("user-agent", "Unknown-Agent/1.0"),
        "trace_id": trace_id,
        "auth_method": auth_method,
        **apigee_ctx,
    }


def normalize_phone_digits(phone: Optional[str]) -> str:
    """
    Summary:
        Extracts raw normalized digits from any phone format for deterministic subscriber matching.
        Example: '+1 (555) 234-5678' -> '5552345678'.

    Parameters:
        phone (Optional[str]): Formatted or raw phone number.

    Return Value:
        str: Cleaned digits string (empty string when phone is None/blank).

    Exceptions/Errors:
        None.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    # Strip the North American country code so '+1 555...' and '555...' match the same row.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits
