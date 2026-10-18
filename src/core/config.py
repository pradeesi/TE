"""
Purpose: Centralized application configuration and environment validation for HybridMCP-Apigee.
Architecture/Context: Imported across the entire codebase (MCP server, CRM console, audit shipper,
                      database layer) to access typed runtime settings. The keys mirror the
                      configuration contract documented in the repository-level .env.example.
Dependencies/Side Effects: Loads values from environment variables or a local .env file with
                           defensive defaults. In Cloud Run, DATABASE_URL is injected from
                           Secret Manager and points at the Cloud SQL unix socket. There is NO
                           application-level auth token: Cloud Run IAM (roles/run.invoker) is the
                           only gate in front of the MCP server.
"""

from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Allowed values for LOKI_AUTH_MODE. 'google_id_token' is used when Loki runs as an
# IAM-protected Cloud Run service and every push must carry a Google-signed ID token.
LOKI_AUTH_MODES = ("none", "google_id_token")


class Settings(BaseSettings):
    """
    Summary:
        System settings model loaded from environment variables.
        Validates configuration boundaries and provides sensible defaults for both
        containerized (Cloud Run / docker-compose) and bare-metal environments.

    Parameters:
        None directly; every attribute is populated from an environment variable of the
        same name (or from .env), falling back to the declared default.

    Return Value:
        Settings: Immutable-by-convention settings object.

    Exceptions/Errors:
        pydantic.ValidationError: Raised at import time if a value has the wrong type or
        LOKI_AUTH_MODE is not one of the supported modes.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Core Application Settings
    APP_NAME: str = "Telecom HybridMCP-Apigee Platform"
    APP_ENV: str = "development"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    # Logical service name used as the 'app' label on Loki audit streams so dashboards can
    # distinguish the MCP server from the CRM console (both share the same image).
    SERVICE_NAME: str = "telecom-mcp-server"

    # Network Ports
    PORT: Optional[int] = None
    CRM_PORT: int = 8000
    MCP_PORT: int = 8001

    # Audit identity fallback used when no end-user identity header or JWT claim is present.
    DEFAULT_END_USER_EMAIL: str = "unknown-user@hybridmcp.invalid"

    # Database Configuration
    # Uses asyncpg driver for PostgreSQL. In Cloud Run the URL targets the built-in Cloud SQL
    # unix socket: postgresql+asyncpg://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE
    DATABASE_URL: str = "postgresql+asyncpg://telecom_user:telecom_secure_pass@localhost:5432/telecom_db"
    # When False, a PostgreSQL failure is fatal instead of silently switching to SQLite.
    # The cloud deployment forces False so MCP and CRM never diverge onto separate stores.
    DB_ALLOW_SQLITE_FALLBACK: bool = True

    # Observability Configuration
    LOKI_URL: Optional[str] = "http://localhost:3100"
    LOKI_AUTH_MODE: str = "none"
    PROMETHEUS_METRICS_ENABLED: bool = True

    @field_validator("LOKI_AUTH_MODE", mode="before")
    @classmethod
    def _validate_loki_auth_mode(cls, value: object) -> str:
        """
        Summary:
            Normalizes and validates the Loki authentication mode.

        Parameters:
            value (object): Raw value read from the environment (None/blank means 'none').

        Return Value:
            str: Lower-cased, validated mode ('none' or 'google_id_token').

        Exceptions/Errors:
            ValueError: If the value is not one of LOKI_AUTH_MODES.
        """
        normalized = str(value or "none").strip().lower()
        if normalized not in LOKI_AUTH_MODES:
            raise ValueError(
                f"LOKI_AUTH_MODE must be one of {LOKI_AUTH_MODES}, got '{value}'."
            )
        return normalized

    @field_validator("DEFAULT_END_USER_EMAIL", "SERVICE_NAME", mode="before")
    @classmethod
    def _validate_non_blank(cls, value: object) -> str:
        """
        Summary:
            Rejects blank identity/label strings that would produce ambiguous audit records.

        Parameters:
            value (object): Raw environment value.

        Return Value:
            str: Stripped non-empty string.

        Exceptions/Errors:
            ValueError: If the value is empty or whitespace only.
        """
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Value must be a non-empty string.")
        return normalized


# Global singleton settings instance
settings = Settings()
