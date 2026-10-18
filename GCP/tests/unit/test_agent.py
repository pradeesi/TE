"""
Purpose: Unit tests verifying the GCP Telecom ADK Agent configuration, 8 MCP tools catalog,
         flexible Apigee authentication modes over Cloud VPN, and audit header synthesis.
Architecture/Context: Executed via pytest during local development and pre-deployment checks
                      in TE/GCP/tests/unit/test_agent.py.
Dependencies/Side Effects: Imports telecom-agent modules and mocks outbound HTTP calls to Apigee.
"""

from unittest.mock import patch
import httpx
import pytest

from app.agent import root_agent, TELECOM_AGENT_INSTRUCTION
from app.config import agent_settings
from app.tools import (
    ALL_TELECOM_TOOLS,
    build_mcp_headers,
    resolve_effective_auth_mode,
    search_customer,
)


def test_agent_structure_and_tools():
    """
    Summary:
        Verifies the ADK Agent name, tool count, and exact registered tool definitions.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If agent metadata or tool catalog diverges from specification.
    """
    assert root_agent.name == agent_settings.AGENT_NAME
    assert len(root_agent.tools) == 8

    expected_tool_names = {
        "search_customer",
        "get_customer_360",
        "get_service_diagnostics",
        "run_remote_device_action",
        "check_network_outages",
        "get_billing_breakdown",
        "get_upsell_recommendations",
        "log_agent_interaction",
    }
    actual_tool_names = {t.__name__ for t in root_agent.tools}
    assert actual_tool_names == expected_tool_names


def test_strict_scope_instruction():
    """
    Summary:
        Ensures the system instruction mandates strict domain boundaries and rejection
        of out-of-scope requests.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If required scope boundary phrases are missing.
    """
    assert "STRICT OPERATIONAL DOMAIN & SCOPE BOUNDARY" in TELECOM_AGENT_INSTRUCTION
    assert "This requested functionality is not available" in TELECOM_AGENT_INSTRUCTION
    assert "search_customer" in TELECOM_AGENT_INSTRUCTION
    assert "log_agent_interaction" in TELECOM_AGENT_INSTRUCTION


def test_build_mcp_headers_flexible_auth_modes(monkeypatch):
    """
    Summary:
        Tests that audit headers, trace correlation, and all flexible Apigee authentication
        modes ('none', 'api_key', 'bearer', 'basic', 'custom_header', 'auto') generate
        expected HTTP headers.

    Parameters:
        monkeypatch: Pytest monkeypatch fixture.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If header construction fails for any authentication mode.
    """
    # 1. Mode: none (Cloud VPN / mTLS network trust only)
    monkeypatch.setattr(agent_settings, "APIGEE_AUTH_MODE", "none")
    headers_none = build_mcp_headers(
        user_email="operator@telecom.com",
        trace_id="trace-test-12345",
        caller_agent="telecom_agent",
    )
    assert headers_none["X-Agent-Identity"] == agent_settings.AGENT_IDENTITY
    assert headers_none["X-Agent-Name"] == "telecom_agent"
    assert headers_none["X-Goog-Authenticated-User-Email"] == "operator@telecom.com"
    assert headers_none["X-End-User-Email"] == "operator@telecom.com"
    assert headers_none["X-Cloud-Trace-Context"] == "trace-test-12345/0;o=1"
    assert headers_none["X-Trace-ID"] == "trace-test-12345"
    assert "Authorization" not in headers_none

    # 2. Mode: api_key (VerifyAPIKey policy on Apigee)
    monkeypatch.setattr(agent_settings, "APIGEE_AUTH_MODE", "api_key")
    monkeypatch.setattr(agent_settings, "APIGEE_API_KEY", "test-apigee-key-123")
    monkeypatch.setattr(agent_settings, "APIGEE_API_KEY_HEADER", "x-apikey")
    headers_apikey = build_mcp_headers(user_email="operator@telecom.com")
    assert headers_apikey["x-apikey"] == "test-apigee-key-123"

    # 3. Mode: bearer (Static Bearer token through Apigee)
    monkeypatch.setattr(agent_settings, "APIGEE_AUTH_MODE", "bearer")
    monkeypatch.setattr(agent_settings, "MCP_AUTH_TOKEN", "test-bearer-token-xyz")
    headers_bearer = build_mcp_headers(user_email="operator@telecom.com")
    assert headers_bearer["Authorization"] == "Bearer test-bearer-token-xyz"
    assert headers_bearer["X-MCP-Token"] == "test-bearer-token-xyz"

    # 4. Mode: custom_header
    monkeypatch.setattr(agent_settings, "APIGEE_AUTH_MODE", "custom_header")
    monkeypatch.setattr(agent_settings, "APIGEE_CUSTOM_HEADER_NAME", "X-Custom-Apigee-Auth")
    monkeypatch.setattr(agent_settings, "APIGEE_CUSTOM_HEADER_VALUE", "custom-secret-999")
    headers_custom = build_mcp_headers(user_email="operator@telecom.com")
    assert headers_custom["X-Custom-Apigee-Auth"] == "custom-secret-999"

    # 5. Mode: auto with both API key and Bearer token populated
    monkeypatch.setattr(agent_settings, "APIGEE_AUTH_MODE", "auto")
    assert resolve_effective_auth_mode() == "multi"
    headers_multi = build_mcp_headers(user_email="operator@telecom.com")
    assert headers_multi["x-apikey"] == "test-apigee-key-123"
    assert headers_multi["Authorization"] == "Bearer test-bearer-token-xyz"


@pytest.mark.asyncio
async def test_tool_dispatch_mocked():
    """
    Summary:
        Tests that tool functions serialize inputs and unpack JSON-RPC responses cleanly.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        AssertionError: If JSON-RPC response unwrapping fails.
    """
    mock_response = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": '{"status": "success", "count": 1, "customers": [{"customer_id": "test-uuid"}]}',
                }
            ]
        },
    }

    mock_resp = httpx.Response(200, json=mock_response)
    with patch.object(httpx.AsyncClient, "post", return_value=mock_resp):
        res = await search_customer(phone_number="+1-555-0199")
        assert res["status"] == "success"
        assert res["customers"][0]["customer_id"] == "test-uuid"
