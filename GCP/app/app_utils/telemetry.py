"""
Purpose: Configure OpenTelemetry tracing, Cloud Logging, and GenAI prompt/response telemetry
         for the Telecom ADK Agent running on GCP Agent Runtime.
Architecture/Context: Attaches Google Cloud Trace, Cloud Logging, and Vertex AI Reasoning Engine
                      telemetry instrumentation to ADK runner calls and MCP tool executions over Cloud VPN.
Dependencies/Side Effects: Sets environment flags for OpenTelemetry GenAI semantic conventions and
                           configures the Vertex AI Agent Runtime tracer and logger providers.

DEMO SOFTWARE DISCLAIMER:
This code is provided strictly as a demonstration and reference implementation.
It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
Use and deployment in any environment is entirely at your own discretion and risk.
"""

import logging
import os


def setup_telemetry() -> str | None:
    """
    Summary:
        Configures OpenTelemetry GenAI instrumentation, Cloud Logging level, and optional
        GCS prompt/response completion uploads for Agent Runtime observability.

    Parameters:
        None.

    Return Value:
        str | None: The configured GCS logs bucket name if set, otherwise None.

    Exceptions/Errors:
        None.
    """
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Enable Vertex AI Agent Runtime (Reasoning Engine) telemetry by default
    os.environ.setdefault("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true")
    os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "false")
    os.environ.setdefault(
        "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental"
    )

    commit_sha = os.environ.get("COMMIT_SHA", "dev")
    os.environ.setdefault(
        "OTEL_RESOURCE_ATTRIBUTES",
        f"service.namespace=telecom-agent,service.name=telecom-agent-hybrid-apigee,service.version={commit_sha}",
    )

    bucket = os.environ.get("LOGS_BUCKET_NAME", "").strip()
    capture_content = os.environ.get(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "EVENT_ONLY"
    ).strip()

    # Normalize legacy boolean strings to valid OpenTelemetry GenAI semconv values
    if capture_content.lower() in ("true", "1"):
        capture_content = "EVENT_ONLY"
    elif capture_content.lower() in ("false", "0"):
        capture_content = "NO_CONTENT"
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = capture_content

    if bucket:
        clean_bucket = bucket.removeprefix("gs://").rstrip("/")
        path = os.environ.get("GENAI_TELEMETRY_PATH", "completions")
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT", "jsonl")
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
            f"gs://{clean_bucket}/{path}",
        )
        logging.info(
            "Agent Runtime prompt-response GCS logging enabled (bucket=gs://%s/%s, mode=%s)",
            clean_bucket,
            path,
            capture_content,
        )
        return clean_bucket

    logging.info(
        "Agent Runtime Cloud Trace & Cloud Logging telemetry enabled (mode=%s; set LOGS_BUCKET_NAME for GCS completion export)",
        capture_content,
    )
    return None


def setup_agent_engine_telemetry() -> None:
    """
    Summary:
        Installs the Vertex AI Agent Runtime (Reasoning Engine) OpenTelemetry tracer
        and Cloud Logging provider so spans (`invoke_agent`, `call_llm`, `execute_tool`)
        and structured logs are tagged with the `aiplatform.googleapis.com/ReasoningEngine`
        monitored resource in the GCP project.

    Parameters:
        None.

    Return Value:
        None.

    Exceptions/Errors:
        None (logs a warning if credentials are unavailable in offline local unit test environments).
    """
    if os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "true").lower() not in (
        "true",
        "1",
    ):
        return

    try:
        import google.auth
        from vertexai.agent_engines.templates.adk import _default_instrumentor_builder

        _, project_id = google.auth.default()
        project_id = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project_id:
            _default_instrumentor_builder(
                project_id,
                enable_tracing=True,
                enable_logging=True,
            )
            logging.info(
                "Vertex AI Agent Runtime telemetry instrumentor initialized for project=%s",
                project_id,
            )
    except Exception as exc:
        logging.debug(
            "Skipping Vertex AI Agent Runtime instrumentor initialization in local environment: %s",
            exc,
        )
