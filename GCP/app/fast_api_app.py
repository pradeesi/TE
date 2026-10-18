"""
Purpose: FastAPI application entrypoint for the GCP Telecom Customer Care ADK Agent.
Architecture/Context: Exposes Agent Development Kit (ADK) execution endpoints, Agent-to-Agent (A2A) protocol routes,
                      and Vertex AI Reasoning Engine adapters (`:query` and `:streamQuery`) for Agent Runtime
                      and manual Gemini Enterprise App registration.
Dependencies/Side Effects: Initializes OpenTelemetry (Cloud Trace & GenAI telemetry), Google Cloud Logging,
                           and loads environment variables.

DEMO SOFTWARE DISCLAIMER:
This code is provided strictly as a demonstration and reference implementation.
It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
Use and deployment in any environment is entirely at your own discretion and risk.
"""

import contextlib
import logging
import os
from collections.abc import AsyncIterator

import google.auth
from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.cloud import logging as google_cloud_logging

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.reasoning_engine_adapter import (
    attach_reasoning_engine_routes,
)
from app.app_utils.telemetry import (
    setup_agent_engine_telemetry,
    setup_telemetry,
)
from app.app_utils.typing import Feedback

load_dotenv()
setup_telemetry()
# Must run before get_fast_api_app to register the ReasoningEngine OTel resource provider.
setup_agent_engine_telemetry()

cloud_logger = None
try:
    _, project_id = google.auth.default()
    logging_client = google_cloud_logging.Client(project=project_id)
    cloud_logger = logging_client.logger(__name__)
except Exception as exc:
    logging.debug("Cloud Logging client fallback to standard logging: %s", exc)

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Summary:
        FastAPI lifespan context manager that initializes the shared ADK Runner and attaches
        Agent-to-Agent (A2A) protocol routes (`/.well-known/agent-card.json` and JSON-RPC).

    Parameters:
        app (FastAPI): The FastAPI application instance.

    Return Value:
        AsyncIterator[None]: Lifespan context yield.

    Exceptions/Errors:
        None.
    """
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=False,
    lifespan=lifespan,
)
app.title = "telecom-agent-hybrid-apigee"
app.description = (
    "GCP Agent Runtime API for the Telecom Customer Care ADK Agent "
    "(connected to On-Premises MCP Server via Apigee over Cloud VPN)"
)

# Attach Vertex AI Reasoning Engine routes (/api/reasoning_engine and /api/stream_reasoning_engine)
# used by GCP Agent Runtime, Vertex AI Console Playground, and Gemini Enterprise App.
attach_reasoning_engine_routes(app)


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """
    Summary:
        Collects and logs user feedback from Gemini Enterprise or test harnesses to
        Google Cloud Logging (routed to BigQuery via the feedback log sink).

    Parameters:
        feedback (Feedback): Structured feedback payload (score, text, user_id, session_id).

    Return Value:
        dict[str, str]: Status confirmation dictionary `{"status": "success"}`.

    Exceptions/Errors:
        None.
    """
    if cloud_logger is not None:
        cloud_logger.log_struct(feedback.model_dump(), severity="INFO")
    else:
        logging.info("Feedback recorded: %s", feedback.model_dump_json())
    return {"status": "success"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
