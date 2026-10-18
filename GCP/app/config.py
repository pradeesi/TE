"""
Purpose: Centralized runtime configuration and environment variable validation for the GCP Telecom ADK Agent.
Architecture/Context: Supplies Apigee API proxy endpoint coordinates (accessed over Cloud VPN), flexible Apigee
                      authentication modes (API Key, Bearer Token, Google OIDC ID Token, HTTP Basic, Custom Header,
                      or Unauthenticated/mTLS), Agent Registry metadata, and GCP Agent Runtime telemetry settings.
Dependencies/Side Effects: Reads configuration from system environment variables and an optional local .env file
                           via pydantic-settings. No secrets are hardcoded.
"""

import os
from typing import Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VALID_APIGEE_AUTH_MODES = frozenset({
    "auto",
    "none",
    "api_key",
    "bearer",
    "static_token",
    "google_id_token",
    "oidc",
    "basic",
    "custom_header",
})


class AgentSettings(BaseSettings):
    """
    Summary:
        Typed configuration model managing runtime parameters for the GCP Telecom ADK Agent
        deployed on Vertex AI Agent Runtime and connected to an on-premises Apigee API proxy
        over Cloud VPN.

    Parameters:
        None (instantiated from process environment variables and optional .env file).

    Return Value:
        AgentSettings: Validated runtime settings instance.

    Exceptions/Errors:
        ValueError: Raised if AGENT_NAME is not a valid Python identifier or if
                    APIGEE_AUTH_MODE is set to an unsupported mode.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --------------------------------------------------------------------------
    # Agent Identification & Foundation Model (Preserved from HybridAI)
    # --------------------------------------------------------------------------
    AGENT_NAME: str = "telecom_agent_hybrid_apigee"
    AGENT_DISPLAY_NAME: str = "Telecom Customer Care Agent (Hybrid via Apigee)"
    AGENT_DESCRIPTION: str = (
        "Enterprise AI Agent for frontline telecom support, line diagnostics, billing, "
        "and outages (Deployed on GCP Agent Runtime and connected to the On-Premises "
        "MCP Server via Apigee API Proxy over Cloud VPN)."
    )
    MODEL_NAME: str = "gemini-2.5-flash"

    # --------------------------------------------------------------------------
    # Google Cloud & Vertex AI Agent Runtime Configuration
    # --------------------------------------------------------------------------
    GOOGLE_GENAI_USE_VERTEXAI: bool = True
    GOOGLE_CLOUD_PROJECT: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    GOOGLE_CLOUD_LOCATION: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    # --------------------------------------------------------------------------
    # On-Premises Apigee API Proxy Connection (Over Cloud VPN)
    # --------------------------------------------------------------------------
    MCP_SERVER_URL: str = os.getenv(
        "MCP_SERVER_URL",
        "https://apigee.onprem.internal/telecom-mcp",
    )
    APIGEE_VERIFY_SSL: bool = True
    MCP_TIMEOUT_SECONDS: float = 30.0

    # --------------------------------------------------------------------------
    # Flexible Apigee Authentication Configuration
    # --------------------------------------------------------------------------
    # Modes: 'auto', 'none', 'api_key', 'bearer', 'google_id_token', 'basic', 'custom_header'
    APIGEE_AUTH_MODE: str = "auto"

    # 1. API Key authentication (e.g., x-apikey: <key>)
    APIGEE_API_KEY: str = ""
    APIGEE_API_KEY_HEADER: str = "x-apikey"

    # 2. Bearer / Static Token authentication (backward-compatible with HybridAI)
    MCP_DEPLOYMENT_TARGET: str = "on_prem"
    MCP_AUTH_TOKEN: str = ""
    APIGEE_BEARER_TOKEN: str = ""

    # 3. Google OIDC ID Token authentication
    APIGEE_OIDC_AUDIENCE: Optional[str] = None

    # 4. HTTP Basic authentication
    APIGEE_BASIC_USER: str = ""
    APIGEE_BASIC_PASSWORD: str = ""

    # 5. Custom Header authentication
    APIGEE_CUSTOM_HEADER_NAME: str = ""
    APIGEE_CUSTOM_HEADER_VALUE: str = ""

    # --------------------------------------------------------------------------
    # Audit, Compliance & GCP Agent Runtime Telemetry Settings
    # --------------------------------------------------------------------------
    AGENT_IDENTITY: str = "telecom-customer-care-agent-hybrid-apigee"
    USER_AGENT: str = "Google-ADK-TelecomAgent-Hybrid-Apigee/1.0"
    DEFAULT_END_USER_EMAIL: str = "operator@telecom.enterprise"
    LOG_LEVEL: str = "INFO"

    @field_validator("AGENT_NAME")
    @classmethod
    def validate_agent_name(cls, value: str) -> str:
        """
        Summary:
            Validates that AGENT_NAME is a valid Python identifier as required by Google ADK.

        Parameters:
            value (str): Candidate agent identifier.

        Return Value:
            str: Validated agent identifier string.

        Exceptions/Errors:
            ValueError: If value is not a valid Python identifier.
        """
        cleaned = value.strip()
        if not cleaned.isidentifier():
            raise ValueError(
                f"AGENT_NAME must be a valid Python identifier, got {value!r}."
            )
        return cleaned

    @field_validator("APIGEE_AUTH_MODE")
    @classmethod
    def validate_auth_mode(cls, value: str) -> str:
        """
        Summary:
            Normalizes and validates the configured Apigee authentication mode.

        Parameters:
            value (str): Raw authentication mode string from environment.

        Return Value:
            str: Lowercase validated authentication mode.

        Exceptions/Errors:
            ValueError: If the authentication mode is not in VALID_APIGEE_AUTH_MODES.
        """
        normalized = (value or "auto").strip().lower()
        if normalized not in VALID_APIGEE_AUTH_MODES:
            allowed = ", ".join(sorted(VALID_APIGEE_AUTH_MODES))
            raise ValueError(
                f"Unsupported APIGEE_AUTH_MODE {value!r}. Supported modes: {allowed}"
            )
        return normalized

    @property
    def mcp_endpoint(self) -> str:
        """
        Summary:
            Computes the full JSON-RPC 2.0 HTTP POST endpoint URL on the Apigee proxy.

        Parameters:
            None.

        Return Value:
            str: Full URL ending with `/mcp`.

        Exceptions/Errors:
            None.
        """
        base = self.MCP_SERVER_URL.strip().rstrip("/")
        if base.endswith("/mcp"):
            return base
        return f"{base}/mcp"


# Global singleton settings instance
agent_settings = AgentSettings()
