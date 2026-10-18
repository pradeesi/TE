"""
Purpose: Process-wide ADK session and artifact services shared by every serving surface.
Architecture/Context: Registered under shared:// so ADK web routes, A2A paths, and reasoning engine adapters share a single session store.
Dependencies/Side Effects: Manages in-memory task store and global artifact services.

DEMO SOFTWARE DISCLAIMER:
This code is provided strictly as a demonstration and reference implementation.
It comes with NO WARRANTY, NO GUARANTEE, and NO SUPPORT of any kind, either expressed or implied.
Use and deployment in any environment is entirely at your own discretion and risk.
"""

from __future__ import annotations

import functools
import os

from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import create_session_service_from_options

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"

_AGENT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


@functools.cache
def get_session_service():
    """Process-wide session service shared across every serving surface."""
    if uri := os.environ.get("SESSION_SERVICE_URI"):
        return create_session_service_from_options(
            base_dir=_AGENT_DIR, session_service_uri=uri
        )
    if agent_engine_id := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService

        return VertexAiSessionService(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            # Runtime-injected agent-engine region, not GOOGLE_CLOUD_LOCATION
            # (which agent.py pins to "global").
            location=os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_LOCATION"),
            agent_engine_id=agent_engine_id,
        )
    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    return InMemorySessionService()


@functools.cache
def get_artifact_service():
    """Process-wide artifact service: GCS when a bucket is set, else in-memory."""
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


_registry = get_service_registry()
_registry.register_session_service("shared", lambda uri, **kw: get_session_service())
_registry.register_artifact_service("shared", lambda uri, **kw: get_artifact_service())
